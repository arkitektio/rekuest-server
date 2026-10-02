//! One registration at a time per organization (`facade/registration_lock.py`).
//!
//! A registration writes rows shared by the organization (actions, protocols, collections,
//! bloks) interleaved with the agent's own; two agents of one fleet registering at once could
//! deadlock on them. A transaction-level advisory lock serializes them, released at commit or
//! rollback.

/// `"REKU"`: the advisory lock class the Python server uses too.
pub const REGISTRATION_LOCK_CLASS: i32 = 0x5245_4B55;

/// Take the registration lock for `organization` in this transaction, waiting if needed.
pub async fn lock_organization(
    tx: &mut sqlx::PgConnection,
    organization: i64,
) -> Result<(), sqlx::Error> {
    sqlx::query("SELECT pg_advisory_xact_lock($1, $2)")
        .bind(REGISTRATION_LOCK_CLASS)
        .bind(i32::try_from(organization).unwrap_or(i32::MAX))
        .execute(tx)
        .await?;
    Ok(())
}
