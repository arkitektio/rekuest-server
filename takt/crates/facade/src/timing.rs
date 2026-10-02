//! When a schedule runs: a cron line read in a time zone, or an interval from its creation.

use std::str::FromStr;

use chrono::{DateTime, Duration, LocalResult, TimeZone, Utc};
use chrono_tz::Tz;
use croner::errors::CronError;
use croner::parser::{CronParser, Seconds, Year};
use croner::Cron;

/// A schedule's timing: exactly one of an interval or a cron line, and the zone the line is read in.
#[derive(Debug, Clone, PartialEq)]
pub struct Timing {
    pub interval_seconds: Option<i64>,
    pub cron: Option<String>,
    pub timezone: String,
}

/// The five-field cron line (minute hour day-of-month month day-of-week), as cron reads it:
/// either day field matching is enough when both are restricted.
fn parse(line: &str) -> Result<Cron, String> {
    CronParser::builder()
        .seconds(Seconds::Disallowed)
        .year(Year::Disallowed)
        .build()
        .parse(line)
        .map_err(|_| format!("Not a valid cron line: {line:?}"))
}

fn zone(name: &str) -> Result<Tz, String> {
    Tz::from_str(name).map_err(|_| format!("Unknown timezone: {name:?}"))
}

/// The first instant strictly after `after` at which `cron`, read in `zone`, is due.
///
/// Across a daylight-saving change: a slot in the skipped hour runs at the first instant after
/// it, and a fixed-time slot in the repeated hour runs once, at its first occurrence.
fn cron_slot(cron: &Cron, zone: Tz, after: DateTime<Utc>) -> Result<DateTime<Utc>, CronError> {
    let local = after.with_timezone(&zone);
    let next = cron.find_next_occurrence(&local, false)?;
    if cron.is_time_matching(&next)? {
        return Ok(next.with_timezone(&Utc));
    }
    // Not a time the line names: croner moved a slot out of the skipped hour. That is right
    // only when the line's own next wall-clock time is in that hour; asked from just before
    // the gap, croner also moves slots that are not, which would run a job twice that day.
    let wall = cron.find_next_occurrence(&local.naive_local().and_utc(), false)?;
    Ok(match zone.from_local_datetime(&wall.naive_utc()) {
        LocalResult::None => next.with_timezone(&Utc),
        LocalResult::Single(slot) | LocalResult::Ambiguous(slot, _) => slot.with_timezone(&Utc),
    })
}

impl Timing {
    /// Refuse unless exactly one of interval and cron is set and both it and the zone parse
    /// (`validate_timing`).
    pub fn validate(&self) -> Result<(), String> {
        if self.interval_seconds.is_none() == self.cron.is_none() {
            return Err("A schedule needs exactly one of interval_seconds or cron".into());
        }
        if self.interval_seconds.is_some_and(|seconds| seconds < 1) {
            return Err("interval_seconds must be at least 1".into());
        }
        if let Some(line) = &self.cron {
            parse(line)?;
        }
        zone(&self.timezone).map(|_| ())
    }

    /// The first slot strictly after `after` (`next_slot`).
    ///
    /// Interval slots are aligned to `created_at`, so they do not drift with how late a run
    /// finished. A cron line is read in the schedule's zone, so "0 2 * * *" stays 02:00 local
    /// across daylight-saving changes.
    pub fn next_slot(
        &self,
        created_at: DateTime<Utc>,
        after: DateTime<Utc>,
    ) -> Result<DateTime<Utc>, String> {
        if let Some(line) = &self.cron {
            return cron_slot(&parse(line)?, zone(&self.timezone)?, after)
                .map_err(|e| format!("The cron line {line:?} has no next slot: {e}"));
        }
        let seconds = self
            .interval_seconds
            .filter(|seconds| *seconds >= 1)
            .ok_or("A schedule needs exactly one of interval_seconds or cron")?;
        if after < created_at {
            return Ok(created_at);
        }
        let interval = Duration::seconds(seconds);
        let elapsed = (after - created_at).num_microseconds().unwrap_or(i64::MAX)
            / interval.num_microseconds().unwrap_or(i64::MAX);
        Ok(created_at + interval * (i32::try_from(elapsed).unwrap_or(i32::MAX - 1) + 1))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn cron(line: &str, timezone: &str) -> Timing {
        Timing {
            interval_seconds: None,
            cron: Some(line.into()),
            timezone: timezone.into(),
        }
    }

    fn oracle() -> Value {
        serde_json::from_str(include_str!("../tests/fixtures/cron.json")).unwrap()
    }

    /// Every slot the Python server computed with croniter, before schedules moved here, but
    /// for the repeated hour of a fall-back night: croniter ran a fixed-time job at both of its
    /// occurrences, and this runs it once.
    #[test]
    fn slots_are_where_the_python_server_put_them() {
        let (mut differences, mut once) = (vec![], vec![]);
        for case in oracle()["next"].as_array().unwrap() {
            let (line, timezone) = (
                case["cron"].as_str().unwrap(),
                case["timezone"].as_str().unwrap(),
            );
            let after: DateTime<Utc> = case["after"].as_str().unwrap().parse().unwrap();
            let expected: DateTime<Utc> = case["next"].as_str().unwrap().parse().unwrap();
            let next = cron(line, timezone).next_slot(after, after).unwrap();
            if next == expected {
                continue;
            }
            let zone = super::zone(timezone).unwrap();
            let repeated = zone
                .from_local_datetime(&expected.with_timezone(&zone).naive_local())
                .latest()
                .is_some_and(|latest| latest.with_timezone(&Utc) == expected)
                && zone
                    .from_local_datetime(&expected.with_timezone(&zone).naive_local())
                    .earliest()
                    .is_some_and(|earliest| earliest.with_timezone(&Utc) < expected);
            if repeated && next > expected {
                once.push(format!("{line} in {timezone}"));
            } else {
                differences.push(format!(
                    "{line:?} in {timezone} after {after}: {next}, python {expected}"
                ));
            }
        }
        assert!(
            differences.is_empty(),
            "{} slots differ:\n{}",
            differences.len(),
            differences.join("\n")
        );
        assert_eq!(
            once,
            [
                "0 2 * * * in Europe/Berlin",
                "30 2 * * * in Europe/Berlin",
                "30 1 * * * in America/New_York"
            ]
        );
    }

    /// What croniter called valid is valid here, with three deliberate exceptions.
    #[test]
    fn lines_are_valid_as_python_judged_them() {
        // Refused here: a sixth field (croniter read it as seconds, at the END of the line; no
        // schedule runs by the second) and a range that wraps around. Accepted here: the last
        // weekday of a month.
        let deliberate = [
            ("0 0 * * * *", false),
            ("* * * * * *", false),
            ("0 0 0 * * *", false),
            ("5-1 * * * *", false),
            ("0 0 * * 5L", true),
        ];
        let mut differences = vec![];
        for (line, valid) in oracle()["valid"].as_object().unwrap() {
            let ours = cron(line, "UTC").validate().is_ok();
            let expected = deliberate
                .iter()
                .find(|(known, _)| known == line)
                .map_or(valid.as_bool().unwrap(), |(_, ours)| *ours);
            if ours != expected {
                differences.push(format!("{line:?}: {ours}, python {valid}"));
            }
        }
        assert!(differences.is_empty(), "{}", differences.join("\n"));
    }

    #[test]
    fn refusals_say_what_is_wrong() {
        let timing = |interval, line: Option<&str>, zone: &str| Timing {
            interval_seconds: interval,
            cron: line.map(str::to_owned),
            timezone: zone.into(),
        };
        assert_eq!(
            timing(None, None, "UTC").validate().unwrap_err(),
            "A schedule needs exactly one of interval_seconds or cron"
        );
        assert_eq!(
            timing(Some(60), Some("* * * * *"), "UTC")
                .validate()
                .unwrap_err(),
            "A schedule needs exactly one of interval_seconds or cron"
        );
        assert_eq!(
            timing(Some(0), None, "UTC").validate().unwrap_err(),
            "interval_seconds must be at least 1"
        );
        assert_eq!(
            timing(None, Some("whenever"), "UTC")
                .validate()
                .unwrap_err(),
            "Not a valid cron line: \"whenever\""
        );
        assert_eq!(
            timing(Some(60), None, "Mars/Olympus")
                .validate()
                .unwrap_err(),
            "Unknown timezone: \"Mars/Olympus\""
        );
    }

    /// Interval slots are counted from the schedule's creation, whenever the last run ended.
    #[test]
    fn interval_slots_do_not_drift() {
        let created: DateTime<Utc> = "2026-01-01T00:00:00Z".parse().unwrap();
        let every = Timing {
            interval_seconds: Some(600),
            cron: None,
            timezone: "UTC".into(),
        };
        let slot = |after: &str| every.next_slot(created, after.parse().unwrap()).unwrap();
        assert_eq!(slot("2025-12-31T23:00:00Z"), created);
        assert_eq!(
            slot("2026-01-01T00:00:00Z"),
            "2026-01-01T00:10:00Z".parse::<DateTime<Utc>>().unwrap()
        );
        assert_eq!(
            slot("2026-01-01T00:17:42Z"),
            "2026-01-01T00:20:00Z".parse::<DateTime<Utc>>().unwrap()
        );
        assert_eq!(
            slot("2026-01-01T00:20:00Z"),
            "2026-01-01T00:30:00Z".parse::<DateTime<Utc>>().unwrap()
        );
    }
}
