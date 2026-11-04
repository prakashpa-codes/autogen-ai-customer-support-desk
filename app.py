"""ResolveDesk support dashboard.

    streamlit run app.py        (starts the local backend too if it is not running yet)

Staff sign in, raise tickets on any channel, watch the four AutoGen agents work live,
work escalated tickets (reply, close, reopen), browse the ticket log and analytics.
"""

from __future__ import annotations

import json

import streamlit as st
import streamlit.components.v1 as components

from desk.api import Backend, BackendError
from desk.launcher import ensure_backend
from desk.ui import inject_css
from desk.views import analytics, email_channel, knowledge, new_ticket, tickets

st.set_page_config(page_title="ResolveDesk · Dada Sports Support", page_icon=":material/support_agent:", layout="wide")
inject_css()


@st.cache_resource(show_spinner="Starting the backend…")
def _backend() -> str:
    """Runs once per Streamlit server, not on every rerun."""
    return ensure_backend()


try:
    _backend()
except RuntimeError as exc:
    st.error(str(exc))
    st.stop()

# The session token is kept in a cookie so a page reload does not sign staff out. It is scoped to
# /dashboard (never sent to the storefront or API paths), SameSite=Strict, Secure over HTTPS, and
# expires with the token. On every new browser session the backend re-validates it (/auth/me).
SESSION_COOKIE = "rd_session"
COOKIE_MAX_AGE = 12 * 3600


def _write_cookie(value: str, max_age: int) -> None:
    """Set the session cookie from the browser (Streamlit has no server-side cookie API)."""
    components.html(
        f"""<script>
        const secure = window.parent.location.protocol === "https:" ? "; Secure" : "";
        window.parent.document.cookie = "{SESSION_COOKIE}=" + {json.dumps(value)} +
            "; path=/dashboard; max-age={max_age}; SameSite=Strict" + secure;
        </script>""",
        height=0,
    )


def _sign_in(token: str, username: str) -> None:
    st.session_state.update(token=token, user=username)


def _restore_session() -> None:
    """Reuse a valid token from the session cookie after a reload."""
    if st.session_state.get("signed_out"):
        return  # this tab just signed out; the browser still sends the old cookie until reload
    token = st.context.cookies.get(SESSION_COOKIE)
    if not token:
        return
    try:
        _sign_in(token, Backend(token).me()["username"])
    except BackendError:
        st.session_state["clear_cookie"] = True  # expired or invalid: remove it


def login_screen() -> None:
    if st.session_state.pop("clear_cookie", False):
        _write_cookie("", 0)
    _, centre, _ = st.columns([1, 1.2, 1])
    with centre:
        st.markdown(
            '<div class="rd-brand"><span class="mark">DS</span><h2 style="margin:0">ResolveDesk</h2></div>',
            unsafe_allow_html=True,
        )
        st.caption("Dada Sports customer support · staff sign in")
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Sign in", type="primary", use_container_width=True):
                try:
                    session = Backend().login(username, password)
                except BackendError as exc:
                    st.error(str(exc))
                else:
                    _sign_in(session["token"], session["username"])
                    st.session_state["write_cookie"] = True
                    st.session_state.pop("signed_out", None)
                    st.rerun()


def sign_out() -> None:
    st.session_state.clear()
    st.session_state.update(signed_out=True, clear_cookie=True)
    st.rerun()


def main() -> None:
    if "token" not in st.session_state:
        _restore_session()
    if "token" not in st.session_state:
        login_screen()
        return
    if st.session_state.pop("write_cookie", False):
        _write_cookie(st.session_state["token"], COOKIE_MAX_AGE)

    api = Backend(st.session_state["token"])

    def page(view, title: str, icon: str, default: bool = False) -> st.Page:
        def run() -> None:
            try:
                view.render(api)
            except BackendError as exc:
                if exc.status_code == 401:  # session expired
                    sign_out()
                st.error(str(exc))

        return st.Page(run, title=title, icon=icon, url_path=view.__name__.rsplit(".", 1)[-1], default=default)

    navigation = st.navigation(
        {
            "Support": [
                page(tickets, "Tickets", ":material/receipt_long:", default=True),
                page(new_ticket, "New ticket", ":material/add_comment:"),
            ],
            "Insights": [
                page(analytics, "Analytics", ":material/monitoring:"),
                page(knowledge, "Knowledge base", ":material/menu_book:"),
            ],
            "Channels": [page(email_channel, "Email", ":material/mail:")],
        }
    )

    with st.sidebar:
        st.markdown('<div class="rd-brand"><span class="mark">DS</span><strong>ResolveDesk</strong></div>', unsafe_allow_html=True)
        st.caption("Dada Sports customer support")
        try:
            health = api.health()
            st.caption(f":material/check_circle: Backend online · {health['model']} · {health['team_mode'].replace('_', ' ')}")
        except BackendError:
            st.caption(":material/error: Backend unreachable")
        st.caption(f"Signed in as **{st.session_state.get('user')}**")
        if st.button("Sign out", icon=":material/logout:"):
            sign_out()

    navigation.run()


main()
