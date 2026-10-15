//! PROGRESS fan-out, at most once per task per [`INTERVAL`].
//!
//! An agent may report progress as fast as its loop runs; every report went to every feed of
//! the task's caller (the app re-renders what shows the task on each one) and to its
//! organization's root-task feed. Progress is a level, not a log: only the newest matters. So a
//! report inside the interval is held, and the newest held one is sent when the interval ends.
//! Every report is still recorded; only the fan-out is thinned.
//!
//! Any other event of the task (a yield, a log, the end) first sends the held progress, so the
//! feed never shows a progress after the event that followed it (see
//! `signals::task_event_created`). Per process: an agent's reports all reach one replica.

use std::collections::HashMap;
use std::sync::{LazyLock, Mutex};
use std::time::{Duration, Instant};

use crate::context::Context;

/// The shortest gap between two PROGRESS fan-outs of one task.
pub const INTERVAL: Duration = Duration::from_millis(100);

/// Tasks remembered at most; past it, those quiet for a while are forgotten.
const MAX_TRACKED: usize = 10_000;

#[derive(Debug, Default)]
struct Slot {
    last_sent: Option<Instant>,
    held: Option<i64>,
    armed: bool,
}

static SLOTS: LazyLock<Mutex<HashMap<i64, Slot>>> = LazyLock::new(Default::default);

#[derive(Debug, PartialEq, Eq)]
enum Admit {
    /// Send it now.
    Now,
    /// Held; send whatever is held after this long (the first hold of an interval arms it).
    HeldArm(Duration),
    /// Held behind an already armed send.
    Held,
}

fn admit(slots: &mut HashMap<i64, Slot>, task: i64, event: i64, now: Instant) -> Admit {
    if slots.len() >= MAX_TRACKED {
        slots.retain(|_, slot| {
            slot.armed || slot.last_sent.is_some_and(|last| now.duration_since(last) < Duration::from_secs(60))
        });
    }
    let slot = slots.entry(task).or_default();
    match slot.last_sent {
        Some(last) if now.duration_since(last) < INTERVAL => {
            slot.held = Some(event);
            if slot.armed {
                Admit::Held
            } else {
                slot.armed = true;
                Admit::HeldArm(INTERVAL - now.duration_since(last))
            }
        }
        _ => {
            slot.last_sent = Some(now);
            slot.held = None;
            Admit::Now
        }
    }
}

fn release(slots: &mut HashMap<i64, Slot>, task: i64, now: Instant) -> Option<i64> {
    let slot = slots.get_mut(&task)?;
    slot.armed = false;
    let held = slot.held.take()?;
    slot.last_sent = Some(now);
    Some(held)
}

/// The progress held for a task, taken to be sent before another event of it. The task starts
/// over: its next progress goes out at once.
pub fn take_held(task: i64) -> Option<i64> {
    SLOTS.lock().ok()?.remove(&task).and_then(|slot| slot.held)
}

/// A PROGRESS event was recorded: fan it out now, or hold it for the end of the interval.
pub async fn progress_recorded(ctx: &Context, task: i64, event: i64) {
    let decision = match SLOTS.lock() {
        Ok(mut slots) => admit(&mut slots, task, event, Instant::now()),
        // A poisoned gate gates nothing.
        Err(_) => Admit::Now,
    };
    match decision {
        Admit::Now => crate::signals::task_event_created(ctx, event).await,
        Admit::HeldArm(delay) => {
            let ctx = ctx.clone();
            tokio::spawn(async move {
                tokio::time::sleep(delay).await;
                let held = SLOTS
                    .lock()
                    .ok()
                    .and_then(|mut slots| release(&mut slots, task, Instant::now()));
                if let Some(held) = held {
                    crate::signals::task_event_created(&ctx, held).await;
                }
            });
        }
        Admit::Held => {}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sends_the_first_holds_the_rest_and_releases_the_newest() {
        let mut slots = HashMap::new();
        let t0 = Instant::now();
        assert_eq!(admit(&mut slots, 1, 10, t0), Admit::Now);
        assert!(matches!(admit(&mut slots, 1, 11, t0 + Duration::from_millis(20)), Admit::HeldArm(_)));
        assert_eq!(admit(&mut slots, 1, 12, t0 + Duration::from_millis(40)), Admit::Held);
        // The interval ends: the newest held one goes, once.
        assert_eq!(release(&mut slots, 1, t0 + Duration::from_millis(100)), Some(12));
        assert_eq!(release(&mut slots, 1, t0 + Duration::from_millis(100)), None);
        // A whole interval after the release, the next one goes at once.
        assert_eq!(admit(&mut slots, 1, 13, t0 + Duration::from_millis(250)), Admit::Now);
    }

    #[test]
    fn tasks_do_not_hold_each_other() {
        let mut slots = HashMap::new();
        let t0 = Instant::now();
        assert_eq!(admit(&mut slots, 1, 10, t0), Admit::Now);
        assert_eq!(admit(&mut slots, 2, 20, t0), Admit::Now);
    }

    #[test]
    fn the_arming_delay_is_what_is_left_of_the_interval() {
        let mut slots = HashMap::new();
        let t0 = Instant::now();
        admit(&mut slots, 1, 10, t0);
        assert_eq!(
            admit(&mut slots, 1, 11, t0 + Duration::from_millis(30)),
            Admit::HeldArm(Duration::from_millis(70))
        );
    }
}
