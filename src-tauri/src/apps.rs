//! Headlamp and Grafana, each in its own child webview that keeps its page and history.
use crate::{vm, Demo};
use serde::Deserialize;
use tauri::{
    webview::{Cookie, Webview, WebviewBuilder},
    AppHandle, LogicalPosition, LogicalSize, Manager, Rect, Url, WebviewUrl, Window,
};

const ALL: [App; 2] = [App::Headlamp, App::Grafana];

#[derive(Clone, Copy, PartialEq)]
enum App {
    Headlamp,
    Grafana,
}

impl App {
    fn parse(name: &str) -> Result<Self, String> {
        ALL.into_iter()
            .find(|app| app.label() == name)
            .ok_or_else(|| format!("Unknown app: {name}"))
    }

    fn label(self) -> &'static str {
        match self {
            App::Headlamp => "headlamp",
            App::Grafana => "grafana",
        }
    }

    fn url(self) -> String {
        let port = match self {
            App::Headlamp => vm::HEADLAMP_PORT,
            App::Grafana => vm::GRAFANA_PORT,
        };
        format!("http://127.0.0.1:{port}/")
    }
}

/// Position of the page area, in the logical pixels the UI measures.
#[derive(Deserialize)]
pub struct Bounds {
    x: f64,
    y: f64,
    width: f64,
    height: f64,
}

impl From<Bounds> for Rect {
    fn from(bounds: Bounds) -> Self {
        Rect {
            position: LogicalPosition::new(bounds.x, bounds.y).into(),
            size: LogicalSize::new(bounds.width, bounds.height).into(),
        }
    }
}

fn navigation_script(action: &str) -> Result<&'static str, String> {
    match action {
        "back" => Ok("history.back()"),
        "forward" => Ok("history.forward()"),
        "reload" => Ok("location.reload()"),
        _ => Err(format!("Unknown navigation: {action}")),
    }
}

fn location(url: &Url) -> String {
    url.as_str()
        .strip_prefix(&format!("{}://", url.scheme()))
        .and_then(|rest| rest.find('/').map(|slash| rest[slash..].to_owned()))
        .unwrap_or_else(|| url.to_string())
}

/// Headlamp splits long tokens across cookies and clears unused chunks with empty values.
fn login_cookies<'a>(
    headers: impl IntoIterator<Item = &'a str>,
) -> Result<Vec<Cookie<'static>>, String> {
    let mut cookies = vec![];
    for header in headers {
        let mut cookie = Cookie::parse(header.to_owned()).map_err(|e| e.to_string())?;
        if !cookie.value().is_empty() {
            // Webviews reject host-less cookies, which browsers would derive from the response.
            cookie.set_domain("127.0.0.1");
            cookies.push(cookie);
        }
    }
    if cookies.is_empty() {
        return Err("Headlamp did not accept the login token.".into());
    }
    Ok(cookies)
}

fn headlamp_login(token: &str) -> Result<Vec<Cookie<'static>>, String> {
    let response = reqwest::blocking::Client::builder()
        .no_proxy()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?
        .post(format!("{}clusters/main/set-token", App::Headlamp.url()))
        .json(&serde_json::json!({ "token": token }))
        .send()
        .and_then(|r| r.error_for_status())
        .map_err(|e| e.to_string())?;
    let headers = response.headers().get_all(reqwest::header::SET_COOKIE);
    login_cookies(headers.iter().filter_map(|value| value.to_str().ok()))
}

async fn login(app: &AppHandle, target: App) -> Result<Vec<Cookie<'static>>, String> {
    if target != App::Headlamp {
        return Ok(vec![]);
    }
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let value = app
            .state::<Demo>()
            .0
            .lock()
            .unwrap()
            .request("credentials")?;
        let token = value["token"]
            .as_str()
            .ok_or("Guest returned no login token.")?;
        headlamp_login(token)
    })
    .await
    .map_err(|e| e.to_string())?
}

fn webview(app: &AppHandle, name: &str) -> Result<Webview, String> {
    let label = App::parse(name)?.label();
    app.get_webview(label)
        .ok_or_else(|| format!("{label} is not open."))
}

fn each_open(app: &AppHandle, action: impl Fn(Webview) -> tauri::Result<()>) -> Result<(), String> {
    for open in ALL
        .iter()
        .filter_map(|other| app.get_webview(other.label()))
    {
        action(open).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
pub async fn open_app(
    app: AppHandle,
    window: Window,
    name: String,
    bounds: Bounds,
) -> Result<(), String> {
    let target = App::parse(&name)?;
    each_open(&app, |open| {
        if open.label() == target.label() {
            Ok(())
        } else {
            open.hide()
        }
    })?;
    let rect = Rect::from(bounds);
    if let Some(existing) = app.get_webview(target.label()) {
        existing.set_bounds(rect).map_err(|e| e.to_string())?;
        return existing.show().map_err(|e| e.to_string());
    }
    let cookies = login(&app, target).await?;
    // Incognito keeps the admin token off disk and starts each fresh cluster clean.
    let blank = WebviewUrl::External("about:blank".parse().unwrap());
    let child = window
        .add_child(
            WebviewBuilder::new(target.label(), blank).incognito(true),
            rect.position,
            rect.size,
        )
        .map_err(|e| e.to_string())?;
    for cookie in cookies {
        child.set_cookie(cookie).map_err(|e| e.to_string())?;
    }
    child
        .navigate(Url::parse(&target.url()).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn hide_apps(app: AppHandle) -> Result<(), String> {
    each_open(&app, |open| open.hide())
}

#[tauri::command]
pub async fn close_apps(app: AppHandle) -> Result<(), String> {
    each_open(&app, |open| open.close())
}

#[tauri::command]
pub async fn set_app_bounds(app: AppHandle, name: String, bounds: Bounds) -> Result<(), String> {
    webview(&app, &name)?
        .set_bounds(Rect::from(bounds))
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn navigate_app(app: AppHandle, name: String, action: String) -> Result<(), String> {
    webview(&app, &name)?
        .eval(navigation_script(&action)?)
        .map_err(|e| e.to_string())
}

#[tauri::command]
pub async fn app_location(app: AppHandle, name: String) -> Result<String, String> {
    let url = webview(&app, &name)?.url().map_err(|e| e.to_string())?;
    Ok(location(&url))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apps_are_only_the_known_loopback_interfaces() {
        assert_eq!(
            App::parse("headlamp").unwrap().url(),
            "http://127.0.0.1:18080/"
        );
        assert_eq!(
            App::parse("grafana").unwrap().url(),
            "http://127.0.0.1:18090/"
        );
        assert!(App::parse("https://example.com").is_err());
        assert!(App::parse("main").is_err());
    }

    #[test]
    fn navigation_accepts_only_browser_controls() {
        assert_eq!(navigation_script("back").unwrap(), "history.back()");
        assert_eq!(navigation_script("forward").unwrap(), "history.forward()");
        assert_eq!(navigation_script("reload").unwrap(), "location.reload()");
        assert!(navigation_script("alert(1)").is_err());
    }

    #[test]
    fn location_hides_the_origin_and_keeps_the_page() {
        let url = "http://127.0.0.1:18090/d/ig?orgId=1#panel".parse().unwrap();
        assert_eq!(location(&url), "/d/ig?orgId=1#panel");
        assert_eq!(location(&"http://127.0.0.1:18090/".parse().unwrap()), "/");
    }

    #[test]
    fn login_cookies_keep_chunks_and_scope_and_drop_stale_chunks() {
        let cookies = login_cookies([
            "headlamp-auth-main.0=first; Path=/clusters/main; HttpOnly; SameSite=Strict",
            "headlamp-auth-main.1=second; Path=/clusters/main; HttpOnly; SameSite=Strict",
            "headlamp-auth-main.2=; Path=/clusters/main; Max-Age=0; HttpOnly; SameSite=Strict",
        ])
        .unwrap();
        assert_eq!(cookies.len(), 2);
        assert_eq!(cookies[1].name(), "headlamp-auth-main.1");
        assert_eq!(cookies[1].value(), "second");
        assert_eq!(cookies[1].path(), Some("/clusters/main"));
        assert_eq!(cookies[1].domain(), Some("127.0.0.1"));
        assert_eq!(cookies[1].http_only(), Some(true));
    }

    #[test]
    fn missing_login_cookie_is_an_error() {
        assert!(login_cookies(["headlamp-auth-main.1=; Max-Age=0"]).is_err());
        assert!(login_cookies(std::iter::empty::<&str>()).is_err());
    }
}
