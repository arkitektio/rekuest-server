//! The schema takt is written against.
//!
//! The rekuest server owns the tables and migrates them; takt reads and writes them with its
//! own SQL. `schema-migrations.txt` names the migrations that SQL was written against (the
//! server's test suite keeps it current), and takt serves only a database that has them: one
//! that lacks a column takt names would fail a request at a time instead.
//!
//! A database *ahead* of the list is served: a later migration may only add what takt need
//! not name (every defaulted column has its default in the schema) or change what this build
//! was tested against, which the migration's own release settles.

use std::time::Duration;

use sqlx::PgPool;

/// `(app, name)` of every migration this build needs applied.
pub fn required() -> Vec<(&'static str, &'static str)> {
    include_str!("../../../schema-migrations.txt")
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty() && !line.starts_with('#'))
        .filter_map(|line| line.split_once(' '))
        .collect()
}

/// The required migrations the database has not applied, as `app.name`.
pub async fn missing(db: &PgPool) -> Result<Vec<String>, sqlx::Error> {
    let mut missing = vec![];
    for (app, name) in required() {
        let applied: bool = sqlx::query_scalar(
            "SELECT EXISTS (SELECT 1 FROM django_migrations WHERE app = $1 AND name = $2)",
        )
        .bind(app)
        .bind(name)
        .fetch_one(db)
        .await?;
        if !applied {
            missing.push(format!("{app}.{name}"));
        }
    }
    Ok(missing)
}

/// Wait until the database has every required migration (the server's migration job may run
/// after takt's start).
pub async fn wait_until_migrated(db: &PgPool) {
    loop {
        match missing(db).await {
            Ok(missing) if missing.is_empty() => return,
            Ok(missing) => tracing::warn!(
                "waiting for the rekuest server to migrate: {} not applied",
                missing.join(", ")
            ),
            // A database the server never migrated has no `django_migrations` yet.
            Err(e) => tracing::warn!("waiting for the rekuest server to migrate: {e}"),
        }
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
}

#[cfg(test)]
mod tests {
    #[test]
    fn the_list_names_a_migration_per_app() {
        let required = super::required();
        assert!(required.iter().any(|(app, _)| *app == "facade"));
        assert!(required
            .iter()
            .all(|(_, name)| name.starts_with(char::is_numeric)));
    }
}
