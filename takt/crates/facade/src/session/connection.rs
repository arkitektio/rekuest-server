//! One window's socket, from `HELLO` to close (docs/session-protocol.md §2–§6).
//!
//! One task owns the session: it reads the window's frames, the topics' live messages (each
//! topic has a channel of its own on the channel layer, joined to the groups the GraphQL
//! subscriptions use) and the answers of calls, which run beside it. Everything it pushes that
//! the window must not miss is numbered (`pos`); what the window reports having received moves
//! the cursors redis keeps for a resume.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::Duration;

use axum::extract::ws::{CloseFrame, Message, WebSocket};
use chrono::{DateTime, Utc};
use futures::stream::{SplitStream, StreamExt};
use futures::SinkExt;
use serde_json::{json, Value};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;

use crate::backend;
use crate::caller_context::CallerContext;
use crate::channels;
use crate::consumers::agent_protocol::GROUP_REFRESH;
use crate::context::Context;
use crate::probes::backend::{self as probe_backend, ProbeControl};
use crate::session::calls;
use crate::session::frames::{close, reason, Answer, ClientFrame, Limits, ServerFrame};
use crate::session::registry::{self, Saved};
use crate::session::topics::{self, Bound, Cursor, Topic, Who};

/// What a window must hold to watch.
const READ_SCOPE: &str = "rekuest_read";
/// What a window must hold to call (and one of the roles).
const CALL_SCOPE: &str = "rekuest_root_call";
const CALL_ROLES: &[&str] = &["rekuest_editor"];
/// The scope and the role that stand for every other one of rekuest (`rekuest/contract.py`,
/// declared to authentikate in `facade/schema.py`): they pass every check, as in GraphQL.
const ALL_SCOPE: &str = "rekuest_all";
const ADMIN_ROLE: &str = "rekuest_admin";
/// How long the first frame may take.
const HELLO_TIMEOUT: Duration = Duration::from_secs(10);
/// How often a backed-up window is looked at again.
const RECOVERY_TICK: Duration = Duration::from_secs(1);

enum Out {
    Text(String),
    /// A protocol-level ping: the window's socket answers it itself, even while the window's
    /// own timers are throttled (a minimized Electron window pings about once a minute).
    Ping,
    Close(u16, String),
}

/// What the window's socket brought.
enum Incoming {
    Frame(Result<ClientFrame, serde_json::Error>),
    /// A pong (or ping): proof the window is there, nothing to handle.
    Heard,
}

/// A task that ends when this is dropped: the session's own timers outlive no return path.
struct AbortOnDrop(JoinHandle<()>);

impl Drop for AbortOnDrop {
    fn drop(&mut self) {
        self.0.abort();
    }
}

/// What reaches the session task besides the window's frames.
enum Inbound {
    /// A message of a topic's channel.
    Live {
        topic: String,
        generation: u64,
        message: Value,
    },
    /// A call's answer.
    Answered(Answer),
}

struct Watch {
    bound: Bound,
    channel: String,
    cursor: Cursor,
    generation: u64,
    forwarder: JoinHandle<()>,
    /// State topics: the revision the window holds whole (its snapshot, or where a replay
    /// started), and the revisions sent since: a patch at or below the cursor that is neither
    /// arrived out of order, and the window's state can no longer be patched into shape.
    state_floor: i64,
    state_sent: VecDeque<i64>,
}

/// How many state revisions a watch remembers having sent.
const STATE_SENT_LIMIT: usize = 1024;

/// What a token grants a session.
#[derive(Debug, Clone, Copy)]
struct Grants {
    read: bool,
    call: bool,
    expires: DateTime<Utc>,
}

impl Grants {
    /// As the GraphQL `@auth` directive decides it: scopes only where they are enforced, the
    /// catch-all scope for any scope, the admin role for any role.
    fn of(token: &authentikate::base_models::JwtToken, enforce_scopes: bool) -> Self {
        let scopes = token.scopes();
        let scoped = |scope: &str| {
            !enforce_scopes || scopes.contains(&ALL_SCOPE) || scopes.contains(&scope)
        };
        let admin = token.roles.iter().any(|role| role == ADMIN_ROLE);
        Self {
            read: scoped(READ_SCOPE),
            call: scoped(CALL_SCOPE) && (admin || token.has_any_role(CALL_ROLES)),
            expires: token.exp,
        }
    }
}

/// Serve one window's socket.
pub async fn serve(ctx: Context, socket: WebSocket) {
    let (mut sink, mut stream) = socket.split();
    let queued = Arc::new(AtomicUsize::new(0));
    let (out, mut out_rx) = mpsc::unbounded_channel::<Out>();
    let writer_queued = queued.clone();
    let writer = tokio::spawn(async move {
        while let Some(next) = out_rx.recv().await {
            match next {
                Out::Text(text) => {
                    let size = text.len();
                    let sent = sink.send(Message::Text(text.into())).await.is_ok();
                    writer_queued.fetch_sub(size, Ordering::Relaxed);
                    if !sent {
                        break;
                    }
                }
                Out::Ping => {
                    if sink.send(Message::Ping(Vec::new().into())).await.is_err() {
                        break;
                    }
                }
                Out::Close(code, reason) => {
                    let _ = sink
                        .send(Message::Close(Some(CloseFrame {
                            code,
                            reason: reason.into(),
                        })))
                        .await;
                    break;
                }
            }
        }
    });

    let hello = match tokio::time::timeout(HELLO_TIMEOUT, next_frame(&mut stream)).await {
        Ok(Some(Ok(ClientFrame::Hello {
            token,
            session,
            last_pos,
            client_info,
        }))) => (token, session, last_pos, client_info),
        _ => {
            let _ = out.send(Out::Close(close::PROTOCOL, "expected HELLO".into()));
            drop(out);
            let _ = writer.await;
            return;
        }
    };
    let (token, named, last_pos, client_info) = hello;
    let (principal, caller, grants) = match authenticate(&ctx, &token).await {
        Ok(found) => found,
        Err(code) => {
            let _ = out.send(Out::Close(code, "not signed in".into()));
            drop(out);
            let _ = writer.await;
            return;
        }
    };

    let (inbox_tx, inbox_rx) = mpsc::unbounded_channel();
    let mut session = Session {
        who: Who {
            caller,
            organization: principal.organization.unwrap_or_default(),
            principal,
            session: String::new(),
        },
        ctx,
        grants,
        connection: uuid::Uuid::new_v4().to_string(),
        out,
        queued,
        inbox_tx,
        next_pos: 1,
        acked_pos: 0,
        ring: VecDeque::new(),
        acked: BTreeMap::new(),
        answers: VecDeque::new(),
        watches: HashMap::new(),
        generation: 0,
        probes: Vec::new(),
        backed_up: HashSet::new(),
        taken_over: false,
    };
    if let Err(e) = session.open(named, last_pos, &client_info).await {
        tracing::error!("opening a caller session failed: {e}");
        let _ = session
            .out
            .send(Out::Close(close::UNAVAILABLE, "try again".into()));
    } else {
        session.run(&mut stream, inbox_rx).await;
        session.leave().await;
    }
    drop(session);
    let _ = writer.await;
}

/// The next frame of the window, `None` when the socket ended.
async fn next_incoming(stream: &mut SplitStream<WebSocket>) -> Option<Incoming> {
    match stream.next().await? {
        Ok(Message::Text(text)) => Some(Incoming::Frame(serde_json::from_str(&text))),
        Ok(Message::Close(_)) | Err(_) => None,
        Ok(_) => Some(Incoming::Heard),
    }
}

/// The next frame of the window, `None` when the socket ended.
async fn next_frame(
    stream: &mut SplitStream<WebSocket>,
) -> Option<Result<ClientFrame, serde_json::Error>> {
    loop {
        if let Incoming::Frame(frame) = next_incoming(stream).await? {
            return Some(frame);
        }
    }
}

/// The token's principal, caller and grants, or the close code refusing it.
async fn authenticate(
    ctx: &Context,
    token: &str,
) -> Result<(CallerContext, i64, Grants), u16> {
    let decoded = authentikate::authenticate_token(&ctx.verifier, token)
        .await
        .map_err(|e| {
            tracing::info!("session refused: {e}");
            if e.is_permission_denied() {
                close::UNAUTHORIZED
            } else {
                close::UNAVAILABLE
            }
        })?;
    let identity = authentikate::expand::expand_token_context(&ctx.db, &decoded)
        .await
        .map_err(|e| {
            if e.is_permission_denied() {
                close::UNAUTHORIZED
            } else {
                close::UNAVAILABLE
            }
        })?;
    let principal = CallerContext::load(
        &ctx.db,
        identity.user,
        identity.client,
        Some(identity.organization),
        decoded.roles.clone(),
    )
    .await
    .map_err(|_| close::UNAVAILABLE)?;
    let caller = backend::get_caller_for_context(&ctx.db, &principal)
        .await
        .map_err(|_| close::UNAVAILABLE)?;
    let grants = Grants::of(&decoded, ctx.verifier.settings.enforce_scopes);
    Ok((principal, caller, grants))
}

struct Session {
    ctx: Context,
    who: Who,
    grants: Grants,
    connection: String,
    out: mpsc::UnboundedSender<Out>,
    queued: Arc<AtomicUsize>,
    inbox_tx: mpsc::UnboundedSender<Inbound>,
    next_pos: u64,
    acked_pos: u64,
    /// Every numbered push the window has not reported yet: its topic and the topic's cursor
    /// after it.
    ring: VecDeque<(u64, Option<String>, Option<Cursor>)>,
    /// Each watched topic's cursor as of `acked_pos` (`None`: its snapshot not reported yet).
    acked: BTreeMap<String, Option<Cursor>>,
    /// The answers the window has not reported yet.
    answers: VecDeque<(u64, Answer)>,
    watches: HashMap<String, Watch>,
    generation: u64,
    probes: Vec<String>,
    /// Topics whose items are held back while the window cannot keep up.
    backed_up: HashSet<String>,
    /// Another connection took this session over.
    taken_over: bool,
}

impl Session {
    // -----------------------------------------------------------------------------------------
    // opening and leaving

    async fn open(
        &mut self,
        named: Option<String>,
        last_pos: Option<u64>,
        client_info: &Value,
    ) -> Result<(), sqlx::Error> {
        let grace = chrono::Duration::from_std(self.ctx.settings.session.grace).unwrap_or_default();
        if let Some(named) = named.filter(|named| !named.is_empty()) {
            // Gone within its grace, or still held by a connection not yet seen to be gone (a
            // window back before its dead socket timed out): taken over either way.
            let resumable = registry::load(&self.ctx, &named).await.filter(|saved| {
                saved.caller == self.who.caller
                    && saved
                        .left_at
                        .is_none_or(|left| Utc::now() - left <= grace)
            });
            if let Some(saved) = resumable {
                self.who.session = named;
                registry::open_row(&self.ctx, &self.who.session, self.who.caller, client_info)
                    .await?;
                return self.resume(saved, last_pos.unwrap_or(0)).await;
            }
            // Gone for longer than the grace (or unknown): a new session, which takes over the
            // runs the old one held by their dead man switch.
            self.who.session = uuid::Uuid::new_v4().to_string();
            registry::open_row(&self.ctx, &self.who.session, self.who.caller, client_info).await?;
            let moved = registry::hand_over_deadman(
                &self.ctx,
                &named,
                &self.who.session,
                self.who.caller,
            )
            .await?;
            if moved > 0 {
                tracing::info!(moved, "a returning window took over its dead man switches");
            }
        } else {
            self.who.session = uuid::Uuid::new_v4().to_string();
            registry::open_row(&self.ctx, &self.who.session, self.who.caller, client_info).await?;
        }
        self.claim().await;
        self.welcome(false);
        Ok(())
    }

    async fn resume(&mut self, saved: Saved, last_pos: u64) -> Result<(), sqlx::Error> {
        self.next_pos = saved.next_pos.max(1);
        self.acked_pos = saved.acked_pos;
        self.probes = saved.probes;
        self.claim().await;
        self.welcome(true);
        // What the window may not have received: answered again (it knows them by request).
        for (pos, answer) in saved.answers {
            if pos > last_pos {
                self.answer(answer);
            }
        }
        // Every topic again, from where the window had it.
        for (topic, cursor) in saved.watches {
            self.watch(topic, cursor).await;
        }
        Ok(())
    }

    fn welcome(&self, resumed: bool) {
        let settings = &self.ctx.settings.session;
        self.send(&ServerFrame::Welcome {
            session: self.who.session.clone(),
            resumed,
            limits: Limits {
                session_grace: settings.grace.as_secs(),
                ping_interval: (settings.idle_timeout.as_secs() / 3).max(1),
                replay_max: settings.replay_max,
                deadman_min: settings.deadman_min,
            },
        });
    }

    /// The socket is gone: keep the session for its grace, and end it when nobody took it back.
    async fn leave(&mut self) {
        for (_, watch) in self.watches.drain() {
            drop_watch(&self.ctx, watch).await;
        }
        if self.taken_over {
            return; // the session is another connection's now
        }
        registry::beat(&self.ctx, &self.who.session).await;
        let watched: Vec<String> = self.acked.keys().cloned().collect();
        if !self.persist_watching(Some(Utc::now()), watched, false).await {
            return;
        }
        let ctx = self.ctx.clone();
        let who = self.who.clone();
        let connection = self.connection.clone();
        tokio::spawn(async move {
            tokio::time::sleep(ctx.settings.session.grace + Duration::from_secs(1)).await;
            let Some(saved) = registry::load(&ctx, &who.session).await else {
                return;
            };
            if saved.connection != connection || saved.left_at.is_none() {
                return; // taken back
            }
            for probe in &saved.probes {
                let _ =
                    probe_backend::control(&ctx, &who.principal, probe, ProbeControl::Cancel).await;
            }
            registry::end_row(&ctx, &who.session).await;
            registry::forget(&ctx, &who.session).await;
        });
    }

    /// Keep the session for a resume; false when another connection took it over.
    async fn persist(&self, left_at: Option<DateTime<Utc>>) -> bool {
        let watched: Vec<String> = self.watches.keys().cloned().collect();
        self.persist_watching(left_at, watched, false).await
    }

    /// The first write of a connection that opened or took over the session.
    async fn claim(&self) {
        let watched: Vec<String> = self.watches.keys().cloned().collect();
        self.persist_watching(None, watched, true).await;
    }

    async fn persist_watching(
        &self,
        left_at: Option<DateTime<Utc>>,
        watched: Vec<String>,
        claim: bool,
    ) -> bool {
        let saved = Saved {
            caller: self.who.caller,
            connection: self.connection.clone(),
            left_at,
            next_pos: self.next_pos,
            acked_pos: self.acked_pos,
            watches: watched
                .into_iter()
                .map(|topic| {
                    let cursor = self.acked.get(&topic).copied().flatten();
                    (topic, cursor)
                })
                .collect(),
            answers: self.answers.iter().cloned().collect(),
            probes: self.probes.clone(),
        };
        if claim {
            registry::save(&self.ctx, &self.who.session, &saved).await;
            return true;
        }
        registry::save_held(&self.ctx, &self.who.session, &saved).await
    }

    /// Another connection took the session over: this one ends without touching it.
    fn superseded(&mut self) {
        self.taken_over = true;
        self.send(&ServerFrame::Closing {
            reason: "superseded".into(),
        });
        let _ = self.out.send(Out::Close(close::SUPERSEDED, "taken over".into()));
    }

    // -----------------------------------------------------------------------------------------
    // the loop

    async fn run(
        &mut self,
        stream: &mut SplitStream<WebSocket>,
        mut inbox: mpsc::UnboundedReceiver<Inbound>,
    ) {
        let settings = self.ctx.settings.session.clone();
        // `last_seen` on a task of its own: a long replay or a slow query in this loop must
        // not make a connected window look gone to the dead man switch.
        let _beat = AbortOnDrop(tokio::spawn({
            let ctx = self.ctx.clone();
            let session = self.who.session.clone();
            let every = settings.beat;
            async move {
                let mut beat = tokio::time::interval(every);
                beat.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                loop {
                    beat.tick().await;
                    registry::beat(&ctx, &session).await;
                }
            }
        }));
        let mut pinger = tokio::time::interval((settings.idle_timeout / 3).max(Duration::from_secs(1)));
        let mut check = tokio::time::interval(settings.watch_check);
        let mut refresh = tokio::time::interval(GROUP_REFRESH);
        let mut recovery = tokio::time::interval(RECOVERY_TICK);
        for timer in [&mut pinger, &mut check, &mut refresh, &mut recovery] {
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            timer.reset();
        }
        let mut heard = tokio::time::Instant::now();
        loop {
            let idle = tokio::time::sleep_until(heard + settings.idle_timeout);
            let expiry = (self.grants.expires - Utc::now())
                .to_std()
                .unwrap_or_default();
            tokio::select! {
                incoming = next_incoming(stream) => {
                    let Some(incoming) = incoming else { return };
                    heard = tokio::time::Instant::now();
                    let Incoming::Frame(frame) = incoming else { continue };
                    match frame {
                        Ok(frame) => {
                            if !self.on_frame(frame).await {
                                return;
                            }
                        }
                        Err(e) => self.send(&ServerFrame::Refused {
                            pos: 0,
                            request: None,
                            topic: None,
                            error: format!("A frame this server does not read: {e}"),
                        }),
                    }
                }
                Some(inbound) = inbox.recv() => match inbound {
                    Inbound::Live { topic, generation, message } => {
                        self.on_live(&topic, generation, message).await;
                    }
                    Inbound::Answered(answer) => {
                        if let Answer::Accepted { result, .. } = &answer {
                            if let Some(probe) = result.get("id").and_then(Value::as_str) {
                                if probe.starts_with("p-") && !self.probes.iter().any(|p| p == probe) {
                                    self.probes.push(probe.to_owned());
                                }
                            }
                        }
                        self.answer(answer);
                    }
                },
                _ = pinger.tick() => {
                    let _ = self.out.send(Out::Ping);
                }
                _ = check.tick() => self.check_watches().await,
                _ = refresh.tick() => self.refresh_groups().await,
                _ = recovery.tick() => self.recover().await,
                () = idle => {
                    let _ = self.out.send(Out::Close(close::IDLE, "no ping".into()));
                    return;
                }
                () = tokio::time::sleep(expiry) => {
                    self.send(&ServerFrame::Closing { reason: "token_expired".into() });
                    let _ = self.out.send(Out::Close(close::UNAUTHORIZED, "token expired".into()));
                    return;
                }
            }
        }
    }

    /// One frame of the window; false to close.
    async fn on_frame(&mut self, frame: ClientFrame) -> bool {
        match frame {
            ClientFrame::Hello { .. } => {
                self.send(&ServerFrame::Refused {
                    pos: 0,
                    request: None,
                    topic: None,
                    error: "Already said hello.".into(),
                });
            }
            ClientFrame::Reauth { token } => match authenticate(&self.ctx, &token).await {
                Ok((_, caller, grants)) if caller == self.who.caller => self.grants = grants,
                Ok(_) | Err(_) => {
                    let _ = self
                        .out
                        .send(Out::Close(close::UNAUTHORIZED, "not this session's".into()));
                    return false;
                }
            },
            ClientFrame::Ping { pos } => {
                self.send(&ServerFrame::Pong);
                if let Some(pos) = pos {
                    self.ack(pos);
                    if !self.persist(None).await {
                        self.superseded();
                        return false;
                    }
                }
            }
            ClientFrame::Watch { topic } => {
                if self.watches.contains_key(&topic) {
                    return true;
                }
                if !self.grants.read {
                    self.refuse_watch(&topic, "This sign-in may not read tasks (rekuest_read).");
                    return true;
                }
                self.watch(topic, None).await;
                if !self.persist(None).await {
                    self.superseded();
                    return false;
                }
            }
            ClientFrame::Unwatch { topic } => {
                if let Some(watch) = self.watches.remove(&topic) {
                    drop_watch(&self.ctx, watch).await;
                }
                self.acked.remove(&topic);
                self.backed_up.remove(&topic);
                if !self.persist(None).await {
                    self.superseded();
                    return false;
                }
            }
            call => {
                let Some(request) = call.request().map(str::to_owned) else {
                    return true;
                };
                if !self.grants.call {
                    self.answer(Answer::Refused {
                        request,
                        error: "This sign-in may not start or steer tasks \
                                (rekuest_root_call, and the rekuest_editor role)."
                            .into(),
                    });
                    return true;
                }
                let ctx = self.ctx.clone();
                let who = self.who.clone();
                let inbox = self.inbox_tx.clone();
                tokio::spawn(async move {
                    if let Some(answer) = calls::run(&ctx, &who, call).await {
                        let _ = inbox.send(Inbound::Answered(answer));
                    }
                });
            }
        }
        true
    }

    // -----------------------------------------------------------------------------------------
    // pushing

    fn send(&self, frame: &ServerFrame) {
        if let Ok(text) = serde_json::to_string(frame) {
            self.queued.fetch_add(text.len(), Ordering::Relaxed);
            let _ = self.out.send(Out::Text(text));
        }
    }

    fn take_pos(&mut self) -> u64 {
        let pos = self.next_pos;
        self.next_pos += 1;
        pos
    }

    /// Remember a push for the resume record, keeping the record bounded: a push that falls
    /// out of it leaves its topic to be resent whole on a resume.
    fn record(&mut self, pos: u64, topic: Option<String>, cursor: Option<Cursor>) {
        self.ring.push_back((pos, topic, cursor));
        while self.ring.len() > self.ctx.settings.session.resume_window {
            if let Some((_, Some(topic), _)) = self.ring.pop_front() {
                self.acked.insert(topic, None);
            }
        }
    }

    fn data(&mut self, topic: &str, item: Value, cursor: Cursor) {
        let pos = self.take_pos();
        self.send(&ServerFrame::Data {
            pos,
            topic: topic.to_owned(),
            item,
        });
        self.record(pos, Some(topic.to_owned()), Some(cursor));
        if let Some(watch) = self.watches.get_mut(topic) {
            watch.cursor = cursor;
            if let Cursor::State { rev, .. } = cursor {
                watch.state_sent.push_back(rev);
                if watch.state_sent.len() > STATE_SENT_LIMIT {
                    watch.state_sent.pop_front();
                }
            }
        }
    }

    fn resync(&mut self, topic: &str, reason: &str, snapshot: Value, cursor: Cursor) {
        let pos = self.take_pos();
        self.send(&ServerFrame::Resync {
            pos,
            topic: topic.to_owned(),
            reason: reason.to_owned(),
            snapshot,
        });
        self.record(pos, Some(topic.to_owned()), Some(cursor));
        if let Some(watch) = self.watches.get_mut(topic) {
            watch.cursor = cursor;
            if let Cursor::State { rev, .. } = cursor {
                watch.state_floor = rev;
                watch.state_sent.clear();
            }
        }
    }

    fn answer(&mut self, answer: Answer) {
        let pos = self.take_pos();
        self.send(&answer.clone().numbered(pos));
        self.answers.push_back((pos, answer));
        self.record(pos, None, None);
    }

    fn refuse_watch(&mut self, topic: &str, error: &str) {
        let pos = self.take_pos();
        self.send(&ServerFrame::Refused {
            pos,
            request: None,
            topic: Some(topic.to_owned()),
            error: error.to_owned(),
        });
        self.record(pos, None, None);
    }

    /// The window received everything up to `pos`.
    fn ack(&mut self, pos: u64) {
        if pos <= self.acked_pos {
            return;
        }
        self.acked_pos = pos;
        while self.ring.front().is_some_and(|(at, _, _)| *at <= pos) {
            if let Some((_, Some(topic), cursor)) = self.ring.pop_front() {
                if self.watches.contains_key(&topic) {
                    self.acked.insert(topic, cursor);
                }
            }
        }
        while self.answers.front().is_some_and(|(at, _)| *at <= pos) {
            self.answers.pop_front();
        }
    }

    /// Whether the window is backed up: its topic's items are held back until it drains.
    fn holding_back(&mut self, topic: &str) -> bool {
        if self.backed_up.contains(topic) {
            return true;
        }
        if self.queued.load(Ordering::Relaxed) > self.ctx.settings.session.buffer_max {
            self.backed_up.insert(topic.to_owned());
            return true;
        }
        false
    }

    /// A window that drained gets the topics held back for it again, whole.
    async fn recover(&mut self) {
        if self.backed_up.is_empty()
            || self.queued.load(Ordering::Relaxed) > self.ctx.settings.session.buffer_max / 2
        {
            return;
        }
        let topics: Vec<String> = self.backed_up.drain().collect();
        for topic in topics {
            self.snapshot(&topic, reason::SLOW_CONSUMER).await;
        }
    }

    // -----------------------------------------------------------------------------------------
    // watching

    /// Follow `name`: join its groups first, then send what the window misses (a snapshot, or
    /// with a cursor everything after it), then its live items as they come.
    async fn watch(&mut self, name: String, cursor: Option<Cursor>) {
        let Some(topic) = Topic::parse(&name) else {
            self.refuse_watch(&name, "Not a topic this server knows.");
            return;
        };
        let bound = match topics::bind(&self.ctx, &self.who, topic).await {
            Ok(bound) => bound,
            Err(e) => {
                self.refuse_watch(&name, &e.to_string());
                return;
            }
        };
        let (channel, mut inbox) = match self.ctx.channel_layer.subscribe("specific").await {
            Ok(subscribed) => subscribed,
            Err(e) => {
                tracing::error!("a session channel failed: {e}");
                self.refuse_watch(&name, "The server could not follow this just now.");
                return;
            }
        };
        for group in &bound.groups {
            if let Err(e) = self.ctx.channel_layer.group_add(group, &channel).await {
                tracing::error!(group, "joining a group failed: {e}");
            }
        }
        self.generation += 1;
        let generation = self.generation;
        let forward = self.inbox_tx.clone();
        let topic_name = name.clone();
        let forwarder = tokio::spawn(async move {
            while let Some(message) = inbox.recv().await {
                if forward
                    .send(Inbound::Live {
                        topic: topic_name.clone(),
                        generation,
                        message,
                    })
                    .is_err()
                {
                    return;
                }
            }
        });
        self.watches.insert(
            name.clone(),
            Watch {
                bound,
                channel,
                cursor: Cursor::List,
                generation,
                forwarder,
                state_floor: 0,
                state_sent: VecDeque::new(),
            },
        );
        self.acked.entry(name.clone()).or_insert(None);
        self.catch_up(&name, cursor).await;
    }

    /// Bring a topic up to date from `cursor` (none: its snapshot).
    async fn catch_up(&mut self, name: &str, cursor: Option<Cursor>) {
        let Some(bound) = self.watches.get(name).map(|w| w.bound.clone()) else {
            return;
        };
        let limit = self.ctx.settings.session.replay_max;
        match (&bound.topic, cursor) {
            (Topic::Task(task), Some(Cursor::Seq { confirmed, max })) => {
                match topics::task_events_after(&self.ctx, *task, confirmed, limit).await {
                    Ok(Some(events)) => {
                        // Everything after the cursor was read, so everything up to the last
                        // one read is confirmed (a PROGRESS dropped by coalescing included).
                        let last = events
                            .last()
                            .and_then(|event| topics::event_seq_and_kind(event).0)
                            .unwrap_or(confirmed);
                        let mut cursor = Cursor::Seq { confirmed, max };
                        self.cursor_into(name, cursor);
                        for event in topics::coalesce_progress(events) {
                            cursor = advance(cursor, &event);
                            self.data(name, event, cursor);
                        }
                        self.cursor_into(
                            name,
                            Cursor::Seq {
                                confirmed: confirmed.max(last),
                                max: max.max(last),
                            },
                        );
                    }
                    Ok(None) => self.snapshot(name, reason::TOO_FAR_BEHIND).await,
                    Err(e) => self.failed(name, e),
                }
            }
            (
                Topic::State { .. } | Topic::States(_),
                Some(Cursor::State {
                    session: Some(session),
                    rev,
                }),
            ) => {
                let agent = match &bound.topic {
                    Topic::State { agent, .. } | Topic::States(agent) => *agent,
                    _ => return,
                };
                match topics::current_state_session(&self.ctx, agent).await {
                    Ok(Some(current)) if current == session => {
                        match topics::patches_after(&self.ctx, &bound, session, rev, limit).await {
                            Ok(Some(patches)) => {
                                self.cursor_into(name, Cursor::State { session: Some(session), rev });
                                if let Some(watch) = self.watches.get_mut(name) {
                                    watch.state_floor = watch.state_floor.max(rev);
                                }
                                for patch in patches {
                                    let rev = patch["global_rev"].as_i64().unwrap_or(rev);
                                    self.data(name, patch, Cursor::State { session: Some(session), rev });
                                }
                            }
                            Ok(None) => self.snapshot(name, reason::TOO_FAR_BEHIND).await,
                            Err(e) => self.failed(name, e),
                        }
                    }
                    Ok(_) => self.snapshot(name, reason::AGENT_RESTARTED).await,
                    Err(e) => self.failed(name, e),
                }
            }
            (_, Some(Cursor::List)) => self.snapshot(name, reason::RESUMED).await,
            _ => self.snapshot(name, reason::NEW).await,
        }
    }

    fn cursor_into(&mut self, name: &str, cursor: Cursor) {
        if let Some(watch) = self.watches.get_mut(name) {
            watch.cursor = cursor;
        }
    }

    fn failed(&mut self, name: &str, e: impl std::fmt::Display) {
        tracing::error!(topic = name, "catching a topic up failed: {e}");
        self.refuse_watch(name, "The server could not follow this just now.");
    }

    /// Send a topic whole (`RESYNC`), and start its cursor there.
    async fn snapshot(&mut self, name: &str, why: &str) {
        let Some(bound) = self.watches.get(name).map(|w| w.bound.clone()) else {
            return;
        };
        let limit = self.ctx.settings.session.replay_max;
        let found = match &bound.topic {
            Topic::Task(task) => topics::task_snapshot(&self.ctx, *task, limit)
                .await
                .map_err(|e| e.to_string()),
            Topic::State { .. } | Topic::States(_) => topics::state_snapshot(&self.ctx, &bound)
                .await
                .map_err(|e| e.to_string()),
            _ => topics::list_snapshot(&self.ctx, &self.who, &bound)
                .await
                .map(|snapshot| (snapshot, Cursor::List))
                .map_err(|e| e.to_string()),
        };
        match found {
            Ok((snapshot, cursor)) => self.resync(name, why, snapshot, cursor),
            Err(e) => self.failed(name, e),
        }
    }

    /// One message of a topic's channel.
    async fn on_live(&mut self, name: &str, generation: u64, message: Value) {
        let Some(watch) = self.watches.get(name) else {
            return;
        };
        if watch.generation != generation {
            return;
        }
        let topic = watch.bound.topic.clone();
        let cursor = watch.cursor;
        if self.holding_back(name) {
            return;
        }
        match topic {
            Topic::Task(task) => {
                let Some(event) = kante::channel::payload_of(&message, channels::TASK_EVENT)
                    .and_then(|payload| payload.get("event"))
                    .filter(|event| !event.is_null())
                    .cloned()
                else {
                    return;
                };
                self.on_task_event(name, task, event, cursor).await;
            }
            Topic::TasksMine | Topic::TasksOrg | Topic::Latest { .. } => {
                if let Some(payload) = kante::channel::payload_of(&message, channels::TASK_EVENT) {
                    self.data(name, payload.clone(), Cursor::List);
                }
            }
            Topic::Children(_) => {
                if let Some(payload) = kante::channel::payload_of(&message, channels::CHILD_TASK) {
                    self.data(name, payload.clone(), Cursor::List);
                }
            }
            Topic::AgentTasks(_) => {
                if let Some(payload) = kante::channel::payload_of(&message, channels::AGENT_TASK) {
                    self.data(name, payload.clone(), Cursor::List);
                }
            }
            Topic::Probe(_) => {
                if let Some(payload) = kante::channel::payload_of(&message, channels::PROBE_EVENT) {
                    self.data(name, payload.clone(), Cursor::List);
                }
            }
            Topic::AgentsOrg => {
                let Some(payload) = kante::channel::payload_of(&message, channels::AGENT_UPDATED)
                else {
                    return;
                };
                for change in ["create", "update"] {
                    if let Some(agent) = payload.get(change).and_then(Value::as_i64) {
                        match topics::agent_change(&self.ctx, agent).await {
                            Ok(row) if !row.is_null() => {
                                let mut item = serde_json::Map::new();
                                item.insert(change.to_owned(), row);
                                self.data(name, Value::Object(item), Cursor::List)
                            }
                            Ok(_) => {}
                            Err(e) => tracing::error!("reading an agent failed: {e}"),
                        }
                    }
                }
                if let Some(agent) = payload.get("delete").and_then(Value::as_i64) {
                    self.data(name, json!({"delete": agent.to_string()}), Cursor::List);
                }
            }
            Topic::State { .. } | Topic::States(_) => {
                if kante::channel::payload_of(&message, channels::STATE_UPDATE).is_some() {
                    self.snapshot(name, reason::STATE_REPLACED).await;
                    return;
                }
                let Some(patch) = kante::channel::payload_of(&message, channels::PATCH).cloned()
                else {
                    return;
                };
                let Cursor::State { session, rev } = cursor else {
                    return;
                };
                let patch_session = patch.get("session").and_then(Value::as_i64);
                let patch_rev = patch.get("global_rev").and_then(Value::as_i64).unwrap_or(0);
                if patch_session != session {
                    self.snapshot(name, reason::AGENT_RESTARTED).await;
                    return;
                }
                if patch_rev <= rev {
                    // Late: fine if it was sent (a seam's duplicate) or the snapshot held it;
                    // otherwise it came after a newer one, and patching on is no longer right.
                    let known = self.watches.get(name).is_some_and(|watch| {
                        patch_rev <= watch.state_floor || watch.state_sent.contains(&patch_rev)
                    });
                    if !known {
                        self.snapshot(name, reason::OUT_OF_ORDER).await;
                    }
                    return;
                }
                // An agent's revisions run 1, 2, 3 … across its states: a jump in `states:` is a
                // patch the feed lost, read back before this one (a `state:` follows one state,
                // whose revisions skip the others', so only its check finds a lost one).
                if matches!(topic, Topic::States(_)) && patch_rev > rev + 1 {
                    let Some(session_id) = session else { return };
                    let limit = self.ctx.settings.session.replay_max;
                    let bound = self.watches.get(name).map(|w| w.bound.clone());
                    let Some(bound) = bound else { return };
                    match topics::patches_after(&self.ctx, &bound, session_id, rev, limit).await {
                        Ok(Some(missed)) => {
                            for item in missed {
                                let missed_rev = item["global_rev"].as_i64().unwrap_or(rev);
                                if missed_rev >= patch_rev {
                                    break;
                                }
                                self.data(name, item, Cursor::State { session, rev: missed_rev });
                            }
                        }
                        Ok(None) => {
                            self.snapshot(name, reason::TOO_FAR_BEHIND).await;
                            return;
                        }
                        Err(e) => tracing::error!(topic = name, "filling a state gap failed: {e}"),
                    }
                }
                self.data(name, patch, Cursor::State { session, rev: patch_rev });
            }
        }
    }

    /// A task event: in order, without gaps (§5.3). A PROGRESS between others may be thinned on
    /// the way (the progress gate), so a jump before a PROGRESS is expected; a jump before any
    /// other event is filled from the database first.
    async fn on_task_event(&mut self, name: &str, task: i64, event: Value, cursor: Cursor) {
        let Cursor::Seq { confirmed, max } = cursor else {
            self.data(name, event, cursor);
            return;
        };
        let (seq, kind) = topics::event_seq_and_kind(&event);
        let Some(seq) = seq else {
            // Written outside `insert_event` (never in production): as it comes.
            self.data(name, event, cursor);
            return;
        };
        if kind == "PROGRESS" {
            if seq > max {
                self.data(name, event, Cursor::Seq { confirmed, max: seq });
            }
            return;
        }
        if seq <= confirmed {
            return;
        }
        let mut cursor = Cursor::Seq { confirmed, max };
        if seq > confirmed + 1 {
            match topics::task_gap(&self.ctx, task, confirmed, max, seq - 1).await {
                Ok(missed) => {
                    for item in missed {
                        cursor = advance(cursor, &item);
                        self.data(name, item, cursor);
                    }
                }
                Err(e) => tracing::error!(task, "filling a task's gap failed: {e}"),
            }
        }
        let Cursor::Seq { max, .. } = cursor else {
            return;
        };
        self.data(
            name,
            event,
            Cursor::Seq {
                confirmed: seq,
                max: max.max(seq),
            },
        );
    }

    /// Look for what the live feed lost (§5.3): a task's newest events, a state's patches.
    async fn check_watches(&mut self) {
        let names: Vec<String> = self.watches.keys().cloned().collect();
        for name in names {
            if self.backed_up.contains(&name) {
                continue;
            }
            let Some((bound, cursor)) = self.watches.get(&name).map(|w| (w.bound.clone(), w.cursor))
            else {
                continue;
            };
            match (&bound.topic, cursor) {
                (Topic::Task(task), Cursor::Seq { confirmed, max }) => {
                    let Ok(Some(upto)) = topics::task_event_seq(&self.ctx, *task).await else {
                        continue;
                    };
                    if upto <= confirmed {
                        continue;
                    }
                    match topics::task_gap(&self.ctx, *task, confirmed, max, upto).await {
                        Ok(missed) => {
                            let mut cursor = Cursor::Seq { confirmed, max };
                            for item in missed {
                                cursor = advance(cursor, &item);
                                self.data(&name, item, cursor);
                            }
                            let Cursor::Seq { max, .. } = cursor else { continue };
                            self.cursor_into(&name, Cursor::Seq { confirmed: upto, max: max.max(upto) });
                        }
                        Err(e) => tracing::error!(task, "checking a task failed: {e}"),
                    }
                }
                (Topic::State { .. } | Topic::States(_), Cursor::State { session, rev }) => {
                    self.catch_up(&name, Some(Cursor::State { session, rev })).await;
                }
                _ => {}
            }
        }
    }

    async fn refresh_groups(&self) {
        for watch in self.watches.values() {
            for group in &watch.bound.groups {
                if let Err(e) = self.ctx.channel_layer.group_add(group, &watch.channel).await {
                    tracing::error!(group, "refreshing a group failed: {e}");
                }
            }
        }
    }
}

/// The cursor after sending `event` (a task event).
fn advance(cursor: Cursor, event: &Value) -> Cursor {
    let Cursor::Seq { confirmed, max } = cursor else {
        return cursor;
    };
    let (seq, kind) = topics::event_seq_and_kind(event);
    let Some(seq) = seq else { return cursor };
    if kind == "PROGRESS" {
        Cursor::Seq {
            confirmed,
            max: max.max(seq),
        }
    } else {
        Cursor::Seq {
            confirmed: confirmed.max(seq),
            max: max.max(seq),
        }
    }
}

async fn drop_watch(ctx: &Context, watch: Watch) {
    watch.forwarder.abort();
    for group in &watch.bound.groups {
        let _ = ctx.channel_layer.group_discard(group, &watch.channel).await;
    }
    ctx.channel_layer.unsubscribe(&watch.channel);
}
