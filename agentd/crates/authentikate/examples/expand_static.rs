//! Expand one static token from a config file: `expand_static <config.yaml> <token>`.
//! Used to check that Python and Rust land on the same rows.

#[tokio::main]
async fn main() {
    let mut args = std::env::args().skip(1);
    let (config, token) = (args.next().unwrap(), args.next().unwrap());
    let yaml: serde_json::Value =
        serde_yaml::from_str(&std::fs::read_to_string(config).unwrap()).unwrap();
    let settings =
        authentikate::AuthentikateSettings::prepare(&yaml["authentikate"], true).unwrap();
    let verifier = authentikate::Verifier::new(settings);
    let decoded = authentikate::authenticate_token(&verifier, &token)
        .await
        .unwrap();
    let db = sqlx::PgPool::connect(&std::env::var("AGENTD_TEST_DATABASE_URL").unwrap())
        .await
        .unwrap();
    let before: Option<String> = sqlx::query_scalar(
        "SELECT changed_hash FROM authentikate_user WHERE sub = $1 AND iss = $2",
    )
    .bind(&decoded.sub)
    .bind(&decoded.iss)
    .fetch_optional(&db)
    .await
    .unwrap()
    .flatten();
    let c = authentikate::expand::expand_token_context(&db, &decoded)
        .await
        .unwrap();
    println!(
        "RS {} {} {} {} {} same_hash={}",
        c.user,
        c.organization,
        c.client,
        c.membership,
        decoded.changed_hash(),
        before.as_deref() == Some(decoded.changed_hash().as_str())
    );
}
