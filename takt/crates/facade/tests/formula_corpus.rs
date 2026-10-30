//! Formulas compiled to a PostgreSQL JSONPath answer what the agent and the frontend answer: the
//! corpus is arkitekt-spec's `tests/fixtures/fits_corpus.json`, copied byte for byte. Needs
//! `TAKT_TEST_DATABASE_URL`; skipped without.

use facade::messages::AgentHardware;
use facade::registration::stored_hardware;
use rekuest_core::inputs::{EstimatedRuntimeInputModel, FitsInputModel};
use serde_json::{json, Value};
use sqlx::PgPool;

fn agent_vars(hardware: &Value) -> Value {
    if hardware
        .as_object()
        .is_some_and(|hardware| hardware.is_empty())
    {
        return json!({"agent": {}});
    }
    let hardware: AgentHardware = serde_json::from_value(hardware.clone()).unwrap();
    json!({"agent": stored_hardware(&hardware)})
}

#[tokio::test]
async fn formulas_answer_what_the_corpus_says() {
    let Ok(url) = std::env::var("TAKT_TEST_DATABASE_URL") else {
        eprintln!("skipped: TAKT_TEST_DATABASE_URL is not set");
        return;
    };
    let db = PgPool::connect(&url).await.unwrap();
    let corpus: Value = serde_json::from_str(include_str!("fixtures/fits_corpus.json")).unwrap();

    for row in corpus["fits"].as_array().unwrap() {
        let fit: FitsInputModel =
            serde_json::from_value(json!({"port": "image", "call": row["call"]})).unwrap();
        let path = fit.compile().unwrap();
        let answer: Option<bool> =
            sqlx::query_scalar("SELECT jsonb_path_match($1::jsonb, $2::jsonpath, $3::jsonb, true)")
                .bind(&row["object"])
                .bind(&path)
                .bind(agent_vars(&row["hardware"]))
                .fetch_one(&db)
                .await
                .unwrap_or_else(|e| panic!("{path}: {e}"));
        assert_eq!(
            answer.unwrap_or(false),
            row["expected"].as_bool().unwrap(),
            "{}: {path}",
            row["formula"]
        );
    }

    for row in corpus["estimated_runtime"].as_array().unwrap() {
        let estimate: EstimatedRuntimeInputModel =
            serde_json::from_value(json!({"call": row["call"]})).unwrap();
        let path = estimate.compile().unwrap();
        let answer: Option<f64> = sqlx::query_scalar(
            "SELECT jsonb_path_query_first($1::jsonb, $2::jsonpath, $3::jsonb, true)::float8",
        )
        .bind(&row["object"])
        .bind(&path)
        .bind(agent_vars(&row["hardware"]))
        .fetch_one(&db)
        .await
        .unwrap_or_else(|e| panic!("{path}: {e}"));
        match row["expected"].as_f64() {
            Some(expected) => assert!(
                (answer.unwrap() - expected).abs() < 1e-6,
                "{}: {path} gave {answer:?}",
                row["formula"]
            ),
            None => assert_eq!(answer, None, "{}: {path}", row["formula"]),
        }
    }
}
