"""`facade_action.live_implementations` (and `assignable`, its `> 0`) — kept by the database.

The count is takt's ``AVAILABLE`` (``takt/crates/facade/src/backend.rs``) asked of the action's
implementations: how many sit on an agent that is a webhook or connected. Not the heartbeat term
(``last_seen`` fresh): that is a time, and no trigger fires on one. takt's stale sweep turns a
crashed agent's ``connected`` off and that change counts like any other, so the column lags takt's
own answer by at most the stale window plus one sweep — only for a crash.

Writers only leave a mark. Implementations and agents are written by takt (Rust) and Django alike,
in transactions of every length — a registration can run for seconds. A trigger that recomputed
in the writer's transaction would hold the action rows it touched until that commits: every
connect of another agent on those actions would wait behind it, and a lease claim (agent row, then
action) could deadlock with a registration (action, then agent row). So:

- The triggers append the affected action ids to ``facade_actionlivenessdirty`` — a log with no
  unique constraint and no foreign key, so concurrent writers never wait on one another — and
  ``NOTIFY rekuest_action_liveness`` (sent on commit only, one per transaction).
  facade_agent fires per row only when ``connected`` or ``kind`` changes: a heartbeat costs
  nothing. facade_implementation fires per statement over its transition tables (Postgres allows
  one event per such trigger, hence three); an update counts only when it moved an implementation.
- ``facade_refresh_live_implementations(batch)`` drains them, and never waits: it claims marks
  ``SKIP LOCKED``, locks their actions ``SKIP LOCKED``, recomputes those in a statement of its own
  (a fresh snapshot: every change whose mark it claimed has committed), and puts back the marks of
  actions someone else holds. Any number of takt replicas may run it at once. It returns how many
  actions it recomputed; takt calls it on the notice and on every reaper tick.
- Nothing else writes the column: a guard keeps the stored value through any other update (a
  full ``save()`` of a stale instance included) and starts every new action at 0.

Two foreign keys get the action they need in the database, not only in Django, because takt
deletes rows with its own SQL:

- a blok revision lets go of its task: ``ON DELETE SET NULL`` on ``facade_blokrevision.task_id``;
- an action's own bloks go with their action: ``ON DELETE CASCADE`` on
  ``facade_actionblok.action_id`` (an older takt, after a failed update or a rollback, does not
  know the table and deletes the action alone; the bloks it leaves without an action are
  collected by the next registration that held them).

Hand-written, so re-add all of it whenever the history is regenerated: ``makemigrations`` emits
none of this. In its own migration because Django adds 0001's foreign keys only as that one ends.
"""

from django.db import migrations

#: The count for the action ``x.id``.
COUNT = """(
    SELECT count(*)::integer
      FROM facade_implementation i
      JOIN facade_agent g ON g.id = i.agent_id
     WHERE i.action_id = x.id
       AND (g.kind = 'WEBHOOK' OR g.connected)
)"""

FUNCTIONS = f"""
CREATE OR REPLACE FUNCTION facade_mark_live_implementations(action_ids bigint[]) RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
    IF action_ids IS NULL OR cardinality(action_ids) = 0 THEN
        RETURN;
    END IF;
    INSERT INTO facade_actionlivenessdirty (action_id) SELECT DISTINCT unnest(action_ids);
    PERFORM pg_notify('rekuest_action_liveness', '');
END
$$;

CREATE OR REPLACE FUNCTION facade_refresh_live_implementations(batch integer DEFAULT 500) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    claimed bigint[];
    locked bigint[];
BEGIN
    WITH taken AS (
        DELETE FROM facade_actionlivenessdirty d
         WHERE d.id IN (
               SELECT id FROM facade_actionlivenessdirty ORDER BY id LIMIT batch FOR UPDATE SKIP LOCKED
         )
        RETURNING d.action_id
    )
    SELECT coalesce(array_agg(DISTINCT action_id), '{{}}') INTO claimed FROM taken;
    IF cardinality(claimed) = 0 THEN
        RETURN 0;
    END IF;

    SELECT coalesce(array_agg(id), '{{}}') INTO locked FROM (
        SELECT id FROM facade_action WHERE id = ANY(claimed) ORDER BY id FOR NO KEY UPDATE SKIP LOCKED
    ) free;

    PERFORM set_config('facade.live_implementations_refresh', 'on', true);
    UPDATE facade_action a
       SET live_implementations = r.n
      FROM (SELECT x.id, {COUNT} AS n FROM unnest(locked) AS x(id)) r
     WHERE a.id = r.id
       AND a.live_implementations IS DISTINCT FROM r.n;
    PERFORM set_config('facade.live_implementations_refresh', 'off', true);

    -- Held by someone else right now: theirs to finish, ours to look at again. An action deleted
    -- since its mark is neither held nor here, and its mark ends.
    INSERT INTO facade_actionlivenessdirty (action_id)
    SELECT id FROM facade_action WHERE id = ANY(claimed) AND NOT (id = ANY(locked));
    RETURN cardinality(locked);
END
$$;

CREATE OR REPLACE FUNCTION facade_action_guard_live_implementations() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- No implementation can name an action that does not exist yet.
        NEW.live_implementations := 0;
    ELSIF coalesce(current_setting('facade.live_implementations_refresh', true), 'off') <> 'on' THEN
        NEW.live_implementations := OLD.live_implementations;
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION facade_agent_mark_live_implementations() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM facade_mark_live_implementations(
        ARRAY(SELECT action_id FROM facade_implementation WHERE agent_id = NEW.id)
    );
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION facade_implementation_inserted_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM facade_mark_live_implementations(ARRAY(SELECT action_id FROM new_rows));
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION facade_implementation_deleted_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM facade_mark_live_implementations(ARRAY(SELECT action_id FROM old_rows));
    RETURN NULL;
END
$$;

CREATE OR REPLACE FUNCTION facade_implementation_updated_mark() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM facade_mark_live_implementations(ARRAY(
        SELECT unnest(ARRAY[o.action_id, n.action_id])
          FROM old_rows o JOIN new_rows n ON n.id = o.id
         WHERE o.action_id IS DISTINCT FROM n.action_id OR o.agent_id IS DISTINCT FROM n.agent_id
    ));
    RETURN NULL;
END
$$;

CREATE TRIGGER facade_action_guard_live_implementations_insert
    BEFORE INSERT ON facade_action
    FOR EACH ROW
    EXECUTE FUNCTION facade_action_guard_live_implementations();

CREATE TRIGGER facade_action_guard_live_implementations_update
    BEFORE UPDATE OF live_implementations ON facade_action
    FOR EACH ROW
    WHEN (OLD.live_implementations IS DISTINCT FROM NEW.live_implementations)
    EXECUTE FUNCTION facade_action_guard_live_implementations();

CREATE TRIGGER facade_agent_mark_live_implementations
    AFTER UPDATE OF connected, kind ON facade_agent
    FOR EACH ROW
    WHEN (OLD.connected IS DISTINCT FROM NEW.connected OR OLD.kind IS DISTINCT FROM NEW.kind)
    EXECUTE FUNCTION facade_agent_mark_live_implementations();

CREATE TRIGGER facade_implementation_inserted_mark
    AFTER INSERT ON facade_implementation
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION facade_implementation_inserted_mark();

CREATE TRIGGER facade_implementation_deleted_mark
    AFTER DELETE ON facade_implementation
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION facade_implementation_deleted_mark();

CREATE TRIGGER facade_implementation_updated_mark
    AFTER UPDATE ON facade_implementation
    REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION facade_implementation_updated_mark();
"""

DROP_FUNCTIONS = """
DROP TRIGGER IF EXISTS facade_implementation_updated_mark ON facade_implementation;
DROP TRIGGER IF EXISTS facade_implementation_deleted_mark ON facade_implementation;
DROP TRIGGER IF EXISTS facade_implementation_inserted_mark ON facade_implementation;
DROP TRIGGER IF EXISTS facade_agent_mark_live_implementations ON facade_agent;
DROP TRIGGER IF EXISTS facade_action_guard_live_implementations_update ON facade_action;
DROP TRIGGER IF EXISTS facade_action_guard_live_implementations_insert ON facade_action;
DROP FUNCTION IF EXISTS facade_implementation_updated_mark();
DROP FUNCTION IF EXISTS facade_implementation_deleted_mark();
DROP FUNCTION IF EXISTS facade_implementation_inserted_mark();
DROP FUNCTION IF EXISTS facade_agent_mark_live_implementations();
DROP FUNCTION IF EXISTS facade_action_guard_live_implementations();
DROP FUNCTION IF EXISTS facade_refresh_live_implementations(integer);
DROP FUNCTION IF EXISTS facade_mark_live_implementations(bigint[]);
"""

SET_NULL = """
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = 'facade_blokrevision'::regclass AND c.contype = 'f' AND a.attname = 'task_id';
    EXECUTE format('ALTER TABLE facade_blokrevision DROP CONSTRAINT %I', name);
    ALTER TABLE facade_blokrevision ADD CONSTRAINT facade_blokrevision_task_id_fk_facade_task_id
        FOREIGN KEY (task_id) REFERENCES facade_task (id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;
END $$;
"""

CASCADE = """
DO $$
DECLARE name text;
BEGIN
    SELECT c.conname INTO name
      FROM pg_constraint c
      JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
     WHERE c.conrelid = 'facade_actionblok'::regclass AND c.contype = 'f' AND a.attname = 'action_id';
    EXECUTE format('ALTER TABLE facade_actionblok DROP CONSTRAINT %I', name);
    ALTER TABLE facade_actionblok ADD CONSTRAINT facade_actionblok_action_id_fk_facade_action_id
        FOREIGN KEY (action_id) REFERENCES facade_action (id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
END $$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("facade", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(FUNCTIONS, DROP_FUNCTIONS),
        migrations.RunSQL(SET_NULL, migrations.RunSQL.noop),
        migrations.RunSQL(CASCADE, migrations.RunSQL.noop),
    ]
