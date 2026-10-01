use std::collections::HashMap;

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::error::{AppResult, Fault};

pub const PROFILE: &str = "sf-bge-m3-1024-v1";
pub const DIMENSION: usize = 1024;
pub const API_SCHEMA: &str = "poetry-api-v1";
pub const DATASETS: [&str; 2] = ["yudingquantangshi", "tangshisanbaishou"];
const TABLES: [&str; 29] = [
    "wudai_huajianji",
    "wudai_nantang",
    "yuanqu",
    "tangsong",
    "mengzi",
    "songci",
    "youmengying",
    "yudingquantangshi",
    "caocao",
    "chuci",
    "shuimotangshi",
    "nalanxingde",
    "lunyu",
    "shijing",
    "daxue",
    "zhongyong",
    "baijiaxing",
    "dizigui",
    "guwenguanzhi",
    "qianjiashi",
    "qianziwen",
    "sanzijing_new",
    "sanzijing_traditional",
    "shenglvqimeng",
    "tangshisanbaishou",
    "wenzimengqiu",
    "youxueqionglin",
    "zengguangxianwen",
    "zhuzijiaxun",
];

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Filters {
    pub tables: Vec<String>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Search {
    pub query: Option<String>,
    pub vector: Option<Vec<f32>>,
    pub embedding_profile: Option<String>,
    pub filters: Option<Filters>,
}

impl Search {
    pub fn decode(bytes: &[u8]) -> AppResult<Self> {
        let fields: serde_json::Value =
            serde_json::from_slice(bytes).map_err(|_| Fault::invalid("invalid JSON"))?;
        for field in ["query", "vector"] {
            if fields.get(field).is_some_and(|value| value.is_null()) {
                return Err(Fault::invalid("query and vector must not be null"));
            }
        }
        let mut request: Self =
            serde_json::from_value(fields).map_err(|_| Fault::invalid("invalid request fields"))?;
        request.validate()?;
        Ok(request)
    }

    fn validate(&mut self) -> AppResult<()> {
        if let Some(query) = &mut self.query {
            *query = query.trim().into();
            if query.is_empty() || query.chars().count() > 512 {
                return Err(Fault::invalid("query must contain 1..512 characters"));
            }
        }
        if self
            .embedding_profile
            .as_deref()
            .is_some_and(|value| !value.is_empty() && value != PROFILE)
        {
            return Err(Fault::invalid("embedding_profile_mismatch"));
        }
        if let Some(vector) = &mut self.vector {
            if self.embedding_profile.as_deref() != Some(PROFILE) {
                return Err(Fault::invalid("vector requires embedding_profile"));
            }
            // The Go API normalizes at HTTP decode and at service entry.
            normalize(vector)?;
            normalize(vector)?;
        } else if self.query.is_none() {
            return Err(Fault::invalid("query or vector is required"));
        }
        if let Some(filters) = &mut self.filters {
            if filters.tables.is_empty()
                || filters
                    .tables
                    .iter()
                    .any(|table| !TABLES.contains(&table.as_str()))
            {
                return Err(Fault::invalid("invalid filters.tables"));
            }
            filters.tables.sort();
            filters.tables.dedup();
        }
        Ok(())
    }

    pub fn selected_tables(&self) -> AppResult<Vec<&str>> {
        let tables: Vec<&str> = match &self.filters {
            Some(filters) => filters.tables.iter().map(String::as_str).collect(),
            None => DATASETS.to_vec(),
        };
        if !tables.iter().any(|table| DATASETS.contains(table)) {
            return Err(Fault::missing("no matching poems"));
        }
        Ok(tables)
    }

    pub fn identity(&self) -> String {
        if let Some(vector) = &self.vector {
            let mut digest = Sha256::new();
            for number in vector {
                digest.update(number.to_le_bytes());
            }
            format!("vector:{:x}", digest.finalize())
        } else {
            format!("text:{}", self.query.as_deref().unwrap_or_default())
        }
    }
}

pub fn normalize(vector: &mut [f32]) -> AppResult<()> {
    if vector.len() != DIMENSION || vector.iter().any(|value| !value.is_finite()) {
        return Err(Fault::invalid("vector must contain 1024 finite values"));
    }
    let scale = vector
        .iter()
        .map(|value| f64::from(*value).abs())
        .fold(0.0, f64::max);
    if scale == 0.0 {
        return Err(Fault::invalid("vector must not be zero"));
    }
    let (mut sum, mut compensation) = (0.0_f64, 0.0_f64);
    for number in vector.iter() {
        let scaled = f64::from(*number) / scale;
        let term = scaled * scaled - compensation;
        let next = sum + term;
        compensation = (next - sum) - term;
        sum = next;
    }
    let norm = sum.sqrt();
    for number in vector {
        *number = ((f64::from(*number) / scale) / norm) as f32;
        if *number == 0.0 {
            *number = 0.0;
        }
    }
    Ok(())
}

#[derive(Deserialize)]
pub struct Locator {
    pub dataset: String,
    pub source_row_id: i64,
    pub raw_index: i64,
    pub normalized_index: i64,
    pub work_id: String,
    pub generation: String,
}

impl Locator {
    pub fn validate(&self, generation: &str) -> AppResult<()> {
        validate_work_id(&self.work_id)?;
        if self.generation != generation
            || self.source_row_id <= 0
            || self.raw_index < 0
            || self.normalized_index < 0
            || !DATASETS.contains(&self.dataset.as_str())
            || !self.work_id.starts_with(&format!("{}:", self.dataset))
        {
            return Err(Fault::unavailable("invalid vector locator"));
        }
        Ok(())
    }
}

#[derive(Deserialize)]
pub struct SearchPayload {
    #[serde(flatten)]
    pub locator: Locator,
}

impl SearchPayload {
    pub fn validate(&self, generation: &str) -> AppResult<()> {
        self.locator.validate(generation)
    }
}

#[derive(Deserialize)]
pub struct OriginalPayload {
    pub work_id: String,
    pub generation: String,
    pub poetry_api_schema: String,
    pub poetry_locators: String,
}

impl OriginalPayload {
    pub fn original(self, locator: &Locator, generation: &str) -> AppResult<String> {
        if self.work_id != locator.work_id
            || self.generation != generation
            || self.poetry_api_schema != API_SCHEMA
        {
            return Err(Fault::unavailable("original payload unavailable"));
        }
        let mut originals: HashMap<String, String> = serde_json::from_str(&self.poetry_locators)
            .map_err(|_| Fault::unavailable("invalid original locators"))?;
        let key = format!(
            "{}:{}:{}",
            locator.source_row_id, locator.raw_index, locator.normalized_index
        );
        originals
            .remove(&key)
            .filter(|original| !original.trim().is_empty())
            .ok_or_else(|| Fault::unavailable("original locator unavailable"))
    }
}

#[derive(Deserialize)]
pub struct WorkPayload {
    pub work_id: String,
    pub generation: String,
    pub poetry_api_schema: String,
    pub poetry_work_document: String,
}

impl WorkPayload {
    pub fn document(self, work_id: &str, generation: &str) -> AppResult<String> {
        let dataset = work_id
            .split_once(':')
            .map(|pair| pair.0)
            .unwrap_or_default();
        let prefix = format!("{{\"id\":\"{work_id}\",\"dataset\":\"{dataset}\",");
        // The deployment tool validates the complete canonical JSON before publishing it.
        if self.work_id != work_id
            || self.generation != generation
            || self.poetry_api_schema != API_SCHEMA
            || !self.poetry_work_document.starts_with(&prefix)
            || !self.poetry_work_document.ends_with('}')
        {
            return Err(Fault::unavailable("work payload unavailable"));
        }
        Ok(self.poetry_work_document)
    }
}

pub fn validate_work_id(value: &str) -> AppResult<()> {
    let Some((dataset, row)) = value.split_once(':') else {
        return Err(Fault::missing("work does not exist"));
    };
    let valid = DATASETS.contains(&dataset)
        && row
            .parse::<i64>()
            .is_ok_and(|number| number > 0 && number.to_string() == row);
    if !valid {
        return Err(Fault::missing("work does not exist"));
    }
    Ok(())
}
