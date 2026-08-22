"""Step 5: review every deployment setting, then bundle or publish the project.

The earlier steps stay about the assistant itself. Everything the exported app
needs but the learner does not — access control, branding, endpoints, secrets —
is gathered here, so nobody configures a favicon before knowing whether the
answers are any good.
"""

import os
import tempfile
import time

import streamlit as st

from asklit.config import get_base_url, get_secret_value
from asklit.experiments import parse_model_names
from asklit.github import (
    GitHubError,
    get_authenticated_user,
    poll_device_token,
    publish_directory,
    request_device_code,
)
from asklit.models import normalize_openai_base_url
from asklit.scaffold.bundle import (
    DEFAULT_REPO_NAME,
    create_bundle,
    generate_deployment_secrets,
    zip_directory,
)
from asklit.scaffold.config import (
    DEFAULT_MODEL_NAME,
    ensure_model_defaults,
    normalize_prompt_profiles,
    provider_options,
)
from asklit.scaffold.endpoints import (
    get_endpoint_model_choices,
    render_endpoint_model_status,
)
from asklit.scaffold.knowledge import knowledgebase_document_counts
from asklit.scaffold.step_chat import preview_model_choices
from asklit.scaffold.ui import render_password_hash_setup, session_paths

REQUEST_TIMEOUT_SECONDS = 20
DEFAULT_APIM_DEPLOYMENTS = (
    "gpt-5.4-nano,gpt-5.4-mini,gpt-5.6-sol,deepseek-v4-pro,"
    "grok-4.1-fast-reasoning,llama-4-maverick,kimi-k2.6,mistral-large-3,"
    "phi-4-mini,gpt-4.1-nano,gpt-4.1-mini"
)


def render_export_step():
    """Review final settings, then download a ZIP or publish to GitHub."""
    ensure_model_defaults(st.session_state.app_config)
    st.header("Export your project")
    st.write(
        "Everything below describes the app you are about to publish. Adjust "
        "anything that should differ from the version you have been testing."
    )

    _render_app_settings()
    _render_model_settings()
    _render_access_and_branding()
    _render_prompt_summary()
    _render_knowledge_summary()
    _render_evaluation_summary()
    _render_secrets_panel()

    st.divider()
    st.subheader("Your no-code deployment checklist")
    st.markdown(
        """
1. Click **Connect to GitHub** below and approve AskLit.
2. Create the repository and wait for **Finished publishing files.**
3. Open [Streamlit Community Cloud](https://share.streamlit.io/) and sign in with the same GitHub account.
4. Click **Create app**, choose the new repository, branch **main**, and file **app.py**.
5. In **Advanced settings**, paste your real configuration into **Secrets**, then click **Deploy**.

Never upload `secrets.toml` to GitHub. Streamlit's **Secrets** box is the only place for real keys.
"""
    )
    repo_name = st.text_input("Repository Name", DEFAULT_REPO_NAME)
    private_repo = st.checkbox(
        "Make the GitHub repository private",
        value=False,
        help=(
            "Public repositories work with Streamlit Community Cloud's free "
            "deployment path. Choose private only if your hosting account can "
            "access private repositories."
        ),
    )
    if not private_repo:
        st.warning(
            "This repository will be public. Its generated configuration and "
            "knowledge-base documents will be visible to everyone. Do not publish "
            "confidential, student, client, or secret material."
        )

    left, right = st.columns(2)
    with left:
        _render_zip_download(repo_name)
    with right:
        _render_github_publish(repo_name, private_repo)


def _render_app_settings():
    config = st.session_state.app_config
    with st.expander("App settings", expanded=True):
        config["app"]["title"] = st.text_input(
            "App title",
            config["app"].get("title", "My Knowledge Base"),
            key="export_app_title",
        )
        config["app"]["welcome_message"] = st.text_area(
            "Welcome message",
            config["app"].get("welcome_message", "How can I help you today?"),
            key="export_welcome_message",
        )


def _render_model_settings():
    """Choose the provider, endpoint, and model the exported app will run on."""
    config = st.session_state.app_config
    model_config = config["model"]
    with st.expander("AI model", expanded=True):
        st.caption(
            "The scaffolder's own credentials are never exported. Add your key to "
            "the deployed app's private Streamlit settings."
        )
        previous_provider = model_config.get("provider", "openai")
        options = provider_options(previous_provider)
        provider = st.selectbox(
            "Provider",
            options,
            index=options.index(previous_provider),
            key="export_provider",
            help=(
                "Azure APIM uses a limited gateway credential instead of exposing "
                "a Foundry account key."
            ),
        )
        current_base_url = (
            model_config.get("base_url", "") if provider == previous_provider else ""
        )
        base_url, manual_endpoint = _render_endpoint_controls(
            provider, current_base_url
        )

        current_model = str(model_config.get("name") or "")
        configured_for_provider = (
            [current_model, *parse_model_names(model_config.get("allowed_models", ""))]
            if provider == previous_provider
            else []
        )
        if manual_endpoint:
            model_choices = []
        else:
            model_choices, choice_source, discovery = get_endpoint_model_choices(
                provider,
                configured_for_provider,
                base_url_override=base_url if provider == "azure_apim" else None,
            )
            render_endpoint_model_status(discovery, choice_source)
        if not model_choices and not manual_endpoint:
            model_choices = preview_model_choices(model_config)

        if model_choices:
            model_name = st.selectbox(
                "Default model for the exported app",
                model_choices,
                index=(
                    model_choices.index(current_model)
                    if current_model in model_choices
                    else 0
                ),
                key="export_default_model",
            )
        else:
            model_name = st.text_input(
                "Default model for the exported app",
                value=current_model or DEFAULT_MODEL_NAME,
                key="export_default_model_text",
            )

        allow_user_selection = st.checkbox(
            "Let users choose among approved models in the deployed chat",
            value=model_config.get("allow_user_selection", provider == "azure_apim"),
            key="export_allow_model_selection",
        )
        allowed_models = st.text_area(
            "Approved model names (comma-separated)",
            value=model_config.get(
                "allowed_models",
                DEFAULT_APIM_DEPLOYMENTS if provider == "azure_apim" else "",
            ),
            key="export_allowed_models",
            height=80,
            help=(
                "These govern the deployed app running on your own key, and for "
                "Azure APIM they should match the gateway policy allowlist. They "
                "do not change which models this scaffolder will run."
            ),
        )

        config["model"] = {
            "provider": provider,
            "name": model_name,
            "allow_user_selection": allow_user_selection,
            "allowed_models": allowed_models,
            "base_url": base_url,
            "use_local_embeddings": True,
            "local_embedding_model": "all-MiniLM-L6-v2",
        }


def _render_endpoint_controls(provider, current_base_url):
    """Collect a custom endpoint URL and say whether it disables discovery."""
    if provider == "openai":
        if not st.checkbox(
            "Use a custom OpenAI-compatible endpoint",
            value=bool(current_base_url),
            key="export_use_custom_endpoint",
            help=(
                "Examples include an OpenAI-compatible proxy or Azure's "
                "/openai/v1 endpoint. The generated app will use your own API key."
            ),
        ):
            return "", False
        entered = st.text_input(
            "OpenAI-compatible base URL",
            value=current_base_url,
            placeholder="https://example.com/v1",
            key="export_custom_endpoint_url",
        )
        base_url, endpoint_error = normalize_openai_base_url(entered)
        if endpoint_error:
            st.error(endpoint_error)
        elif base_url.startswith("http://"):
            st.warning(
                "Use HTTPS for a remotely hosted app. HTTP should be limited to "
                "local development endpoints."
            )
        st.caption(
            "For safety, this public scaffolder does not send its API key to "
            "custom endpoints. Enter the model name manually; the exported app "
            "will use this URL with the API key you add to its secrets."
        )
        return base_url, True

    if provider != "azure_apim":
        return "", False

    trusted_gateway_url = str(get_base_url("azure_apim") or "").rstrip("/")
    entered = st.text_input(
        "Azure APIM gateway base URL",
        value=current_base_url or trusted_gateway_url,
        placeholder="https://your-gateway.azure-api.net/asklit",
        key="export_apim_gateway_url",
        help=(
            "The current workshop gateway is prefilled. Change it only when the "
            "generated app should use a different APIM gateway."
        ),
    )
    base_url, endpoint_error = normalize_openai_base_url(entered)
    custom = bool(
        entered
        and (
            endpoint_error
            or not trusted_gateway_url
            or base_url.rstrip("/") != trusted_gateway_url
        )
    )
    if endpoint_error:
        st.error(endpoint_error)
    elif custom:
        st.caption(
            "This URL will be exported, but the scaffolder will not send its "
            "gateway key to an untrusted custom gateway. Enter deployment names "
            "manually."
        )
    else:
        st.caption("Using the scaffolder's current default APIM gateway URL.")
    return base_url, custom


def _render_access_and_branding():
    """Set who may use the deployed app and how it looks."""
    config = st.session_state.app_config
    with st.expander("Access and branding", expanded=False):
        access_options = ["Public", "Password Protected"]
        current_access = config["app"].get("access_mode", "public")
        selected_access = st.selectbox(
            "Who can access the chat?",
            access_options,
            index=0 if current_access == "public" else 1,
            key="export_access_mode",
        )
        config["app"]["access_mode"] = (
            "public" if selected_access == "Public" else "password"
        )
        if config["app"]["access_mode"] == "password":
            render_password_hash_setup(
                "App access password",
                "scaffold_shared_password_hash",
                "Choose the password visitors will use. AskLit immediately hashes "
                "it and does not retain the plain-text password.",
            )
        else:
            st.session_state.pop("scaffold_shared_password_hash", None)

        config["app"]["disable_admin"] = st.checkbox(
            "Disable admin backend",
            config["app"].get("disable_admin", True),
            key="export_disable_admin",
            help="Hide all management and setup pages in the deployed app.",
        )
        if not config["app"]["disable_admin"]:
            render_password_hash_setup(
                "Administrator password",
                "scaffold_admin_password_hash",
                "Choose a separate password for the hidden administration pages. "
                "Only its hash will be placed in deployment secrets.",
            )
        else:
            st.session_state.pop("scaffold_admin_password_hash", None)

        _render_branding_assets()


def _render_branding_assets():
    """Upload or link the logo and favicon, then set the footer."""
    config = st.session_state.app_config
    branding = config.setdefault("branding", {})
    assets_dir = os.path.join(st.session_state.temp_data_dir, "assets")

    for field, label, types in (
        ("logo_url", "Logo", ["png", "jpg", "jpeg", "svg"]),
        ("favicon_url", "Favicon", ["png", "jpg", "ico", "svg"]),
    ):
        uploaded = st.file_uploader(
            f"Upload {label.lower()}", type=types, key=f"export_upload_{field}"
        )
        if uploaded:
            os.makedirs(assets_dir, exist_ok=True)
            with open(os.path.join(assets_dir, uploaded.name), "wb") as handle:
                handle.write(uploaded.getbuffer())
            branding[field] = f"data/assets/{uploaded.name}"
            st.success(f"{label} uploaded: {uploaded.name}")
        else:
            branding[field] = st.text_input(
                f"{label} URL", branding.get(field, ""), key=f"export_{field}"
            )

    branding["homepage_url"] = st.text_input(
        "Homepage URL", branding.get("homepage_url", ""), key="export_homepage_url"
    )
    branding["supplemental_footer_text"] = st.text_input(
        "Supplemental footer text",
        branding.get("supplemental_footer_text", ""),
        key="export_footer_text",
        help="Appears before the 'Made with AskLit' link.",
    )
    branding["hide_asklit_badge"] = st.checkbox(
        "Hide 'Made with AskLit' link",
        branding.get("hide_asklit_badge", False),
        key="export_hide_badge",
    )


def _render_prompt_summary():
    """Last chance to adjust prompt text and starters before publishing."""
    config = st.session_state.app_config
    profiles = normalize_prompt_profiles(config.get("prompt_profiles"))
    config["prompt_profiles"] = profiles
    with st.expander("Prompts and conversation starters", expanded=True):
        for index, profile in enumerate(profiles):
            st.markdown(f"**{profile['label']}** · `{profile['knowledgebase']}`")
            profile["prompt"] = st.text_area(
                f"System prompt — {profile['label']}",
                profile["prompt"],
                key=f"export_prompt_{index}",
                height=140,
            )
            profile["conversation_starters"] = [
                line.strip()
                for line in st.text_area(
                    f"Conversation starters — {profile['label']}",
                    "\n".join(profile.get("conversation_starters", [])),
                    key=f"export_starters_{index}",
                    height=90,
                ).splitlines()
                if line.strip()
            ]


def _render_knowledge_summary():
    db_path, _chroma_path = session_paths()
    counts = knowledgebase_document_counts(db_path)
    with st.expander("Knowledge bases", expanded=False):
        for profile in st.session_state.app_config["prompt_profiles"]:
            files = profile.get("connected_files") or []
            file_text = ", ".join(files) if files else "All indexed files"
            total = counts.get(profile["knowledgebase"], 0)
            st.markdown(
                f"**{profile['label']}** · `{profile['knowledgebase']}` · "
                f"{total} indexed document(s) · {file_text}"
            )
            if not total:
                st.warning(
                    f"`{profile['knowledgebase']}` has no indexed documents, so this "
                    "prompt will ship without a knowledge base."
                )


def _render_evaluation_summary():
    with st.expander("Evaluation", expanded=False):
        scenarios = st.session_state.get("evaluation_scenarios", [])
        rubrics = st.session_state.get("evaluation_rubrics", [])
        results = st.session_state.get("experiment_results", [])
        st.write(f"{len(scenarios)} scenario(s), {len(rubrics)} shared rubric(s)")
        if results:
            graded = sum(1 for result in results if result["passed"] is not None)
            passed = sum(1 for result in results if result["passed"] is True)
            st.write(
                f"Last run: {len(results)} result(s), "
                + (f"{passed / graded:.0%} pass rate." if graded else "none graded.")
            )
        st.caption(
            "Evaluation scenarios and results are not included in the exported app."
        )


def _render_secrets_panel():
    """Show the deployment secrets block and flag any password still missing."""
    config = st.session_state.app_config
    with st.expander("📋 Deployment settings & secrets", expanded=False):
        st.markdown(
            "Paste the following into your **Streamlit Cloud > Settings > Secrets** panel:"
        )
        password_hashes = {
            "shared": st.session_state.get("scaffold_shared_password_hash"),
            "admin": st.session_state.get("scaffold_admin_password_hash"),
        }
        secrets_toml = generate_deployment_secrets(
            config, password_hashes=password_hashes
        )

        missing_passwords = []
        if config["app"]["access_mode"] == "password" and not password_hashes["shared"]:
            missing_passwords.append("app access password")
        if not config["app"].get("disable_admin") and not password_hashes["admin"]:
            missing_passwords.append("administrator password")
        if missing_passwords:
            st.warning(
                "Open **Access and branding** above and configure: "
                + ", ".join(missing_passwords)
                + "."
            )

        st.code(secrets_toml, language="toml")
        st.download_button(
            "Download deployment secrets",
            data=secrets_toml,
            file_name="asklit-deployment-secrets.toml",
            mime="text/plain",
            help=(
                "This contains password hashes and an API-key placeholder. "
                "Keep it out of GitHub."
            ),
        )


def _render_zip_download(repo_name):
    """Bundle the project so it can be uploaded to GitHub by hand."""
    st.subheader("Option A: Download ZIP")
    st.write(
        "Download the complete project folder, ready to be uploaded to GitHub manually."
    )
    if st.button("Prepare ZIP Download"):
        with st.spinner("Bundling files..."):
            bundle_dir = create_bundle(
                st.session_state.app_config, st.session_state.temp_data_dir
            )
            zip_path = os.path.join(tempfile.gettempdir(), f"{repo_name}.zip")
            zip_directory(bundle_dir, zip_path)
            with open(zip_path, "rb") as handle:
                st.session_state["scaffold_zip"] = {
                    "name": f"{repo_name}.zip",
                    "data": handle.read(),
                }

    bundle = st.session_state.get("scaffold_zip")
    if bundle:
        # Held in session state so the download button survives the rerun that
        # clicking it triggers.
        st.download_button(
            label="📥 Download Project ZIP",
            data=bundle["data"],
            file_name=bundle["name"],
            mime="application/zip",
        )


def _render_github_publish(repo_name, private_repo):
    """Run the GitHub device-code flow and publish the bundle."""
    st.subheader("Option B: Push to GitHub")
    client_id = get_secret_value("GITHUB_CLIENT_ID", None)
    if not client_id:
        st.error(
            "The centralized GitHub connection is temporarily unavailable. "
            "Please contact the AskLit administrator; you should never need to "
            "create a personal access token."
        )
        return

    github_token = st.session_state.get("github_oauth_token")
    device = st.session_state.get("github_oauth_device")

    if device and time.time() >= device["expires_at"]:
        del st.session_state.github_oauth_device
        device = None
        st.warning("The GitHub connection code expired. Start again below.")

    if not github_token and not device:
        if st.button("🔗 Connect to GitHub", type="primary"):
            try:
                device = request_device_code(
                    client_id, timeout=REQUEST_TIMEOUT_SECONDS
                )
                device["expires_at"] = time.time() + int(device["expires_in"])
                st.session_state.github_oauth_device = device
                st.rerun()
            except GitHubError as exc:
                st.error(str(exc))

    if not github_token and device:
        st.info(
            "Open GitHub, enter the one-time code below, and approve "
            "private-repository access. Keep this page open."
        )
        st.code(device["user_code"], language=None)
        st.link_button(
            "Open GitHub authorization", device["verification_uri"], type="primary"
        )
        if st.button("I've authorized GitHub — connect"):
            try:
                result = poll_device_token(
                    client_id, device["device_code"], timeout=REQUEST_TIMEOUT_SECONDS
                )
                if result["status"] == "complete":
                    st.session_state.github_oauth_token = result["access_token"]
                    st.session_state.pop("github_oauth_device", None)
                    st.rerun()
                elif result["status"] == "pending":
                    st.info(
                        "GitHub has not received the approval yet. "
                        "Approve it there, then try this button again."
                    )
                else:
                    st.session_state.pop("github_oauth_device", None)
                    st.error(result["message"])
            except GitHubError as exc:
                st.error(str(exc))

    if not github_token:
        return

    try:
        github_user = st.session_state.get("github_oauth_user")
        if not github_user:
            github_user = get_authenticated_user(
                github_token, timeout=REQUEST_TIMEOUT_SECONDS
            )
            st.session_state.github_oauth_user = github_user
        st.success(f"Connected to GitHub as {github_user['login']}.")
    except GitHubError as exc:
        st.session_state.pop("github_oauth_token", None)
        st.session_state.pop("github_oauth_user", None)
        st.error(str(exc))
        return

    if st.button("Disconnect GitHub"):
        st.session_state.pop("github_oauth_token", None)
        st.session_state.pop("github_oauth_user", None)
        st.rerun()

    if not st.button("🚀 Create Repo & Push"):
        return
    with st.spinner("Creating repository..."):
        bundle_dir = create_bundle(
            st.session_state.app_config, st.session_state.temp_data_dir
        )
        publish_progress = st.progress(0.0, text="Preparing files...")

        def update_publish_progress(completed, total, path):
            publish_progress.progress(
                completed / total, text=f"Publishing {path} ({completed}/{total})"
            )

        try:
            result = publish_directory(
                github_token,
                repo_name,
                bundle_dir,
                private=private_repo,
                timeout=REQUEST_TIMEOUT_SECONDS,
                progress=update_publish_progress,
            )
            publish_progress.progress(1.0, text="Finished publishing files.")
            st.success(
                f"Published {result['files_published']} files to {result['full_name']}."
            )
            st.link_button(
                "Open the new GitHub repository", result["html_url"], type="primary"
            )
        except GitHubError as exc:
            st.error(str(exc))
            st.caption(
                "If GitHub created the repository before an upload failed, open "
                "GitHub to review or delete the partial repository."
            )
