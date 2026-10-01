pub type AppResult<T> = std::result::Result<T, Fault>;

#[derive(Debug)]
pub struct Fault {
    pub status: u16,
    pub detail: &'static str,
}

impl Fault {
    pub fn invalid(detail: &'static str) -> Self {
        Self {
            status: 422,
            detail,
        }
    }

    pub fn unavailable(detail: &'static str) -> Self {
        Self {
            status: 503,
            detail,
        }
    }

    pub fn missing(detail: &'static str) -> Self {
        Self {
            status: 404,
            detail,
        }
    }
}

impl From<worker::Error> for Fault {
    fn from(_: worker::Error) -> Self {
        Self::unavailable("service unavailable")
    }
}
