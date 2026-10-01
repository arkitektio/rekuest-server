//! The migrations agentd names are applied in the database its tests run against: the stack is
//! migrated by this checkout, so a list that is ahead of (or misspells) the server fails here.
//! Needs `AGENTD_TEST_DATABASE_URL`; skipped without it.

#[tokio::test]
async fn the_test_database_has_every_required_migration() {
    let Ok(url) = std::env::var("AGENTD_TEST_DATABASE_URL") else {
        return;
    };
    let db = sqlx::PgPool::connect(&url).await.unwrap();
    assert!(!facade::schema::required().is_empty());
    assert_eq!(
        facade::schema::missing(&db).await.unwrap(),
        Vec::<String>::new()
    );
}

/// A fresh agent in its own organization, registered with a predicate in a collection:
/// (the collection's id, the protocol's id).
async fn predicate_in(db: &sqlx::PgPool, collection: &str) -> (i64, i64) {
    use serde_json::json;
    let unique = uuid::Uuid::new_v4().simple().to_string();
    let spec: authentikate::base_models::StaticToken = serde_json::from_value(json!({
        "sub": format!("s-{unique}"), "iss": "schema-tests", "org": format!("schema-{unique}"),
        "client_id": format!("c-{unique}"), "client_app": format!("a-{unique}"), "client_release": "1",
    }))
    .unwrap();
    let identity =
        authentikate::expand::expand_token_context(db, &spec.to_token(chrono::Utc::now(), "raw"))
            .await
            .unwrap();
    let agent = facade::registration::ensure_agent(
        db,
        identity.client,
        identity.user,
        identity.organization,
    )
    .await
    .unwrap();
    let payload: rekuest_core::inputs::ImplementAgentInputModel = serde_json::from_value(json!({
        "hash": unique,
        "implementations": [{
            "interface": "is_sharp",
            "definition": {"key": "is_sharp", "name": "Is sharp", "kind": "FUNCTION", "collections": [collection],
                           "args": [], "returns": [{"key": "sharp", "kind": "BOOL", "nullable": false}]},
        }],
    }))
    .unwrap();
    let mut tx = db.begin().await.unwrap();
    facade::registration::implement_agent(&mut tx, agent, &payload)
        .await
        .unwrap();
    tx.commit().await.unwrap();
    sqlx::query_as(
        "SELECT c.id, p.id FROM facade_implementation i
           JOIN facade_action_collections ac ON ac.action_id = i.action_id
           JOIN facade_collection c ON c.id = ac.collection_id AND c.organization_id = $2
           JOIN facade_action_protocols ap ON ap.action_id = i.action_id
           JOIN facade_protocol p ON p.id = ap.protocol_id AND p.organization_id = $2
          WHERE i.agent_id = $1",
    )
    .bind(agent)
    .bind(identity.organization)
    .fetch_one(db)
    .await
    .expect("the action is in its own organization's collection and protocol")
}

/// Protocol and collection names were unique across organizations: a second organization's first
/// predicate was refused, and its action joined the first organization's collection.
#[tokio::test]
async fn two_organizations_each_have_their_own_protocols_and_collections() {
    let Ok(url) = std::env::var("AGENTD_TEST_DATABASE_URL") else {
        return;
    };
    let db = sqlx::PgPool::connect(&url).await.unwrap();
    let collection = format!("quality-{}", uuid::Uuid::new_v4().simple());

    let first = predicate_in(&db, &collection).await;
    let second = predicate_in(&db, &collection).await;

    assert_ne!(
        first.0, second.0,
        "each organization has its own collection of that name"
    );
    assert_ne!(first.1, second.1, "and its own `predicate` protocol");
}
