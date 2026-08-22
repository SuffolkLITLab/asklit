"""Model discovery for the scaffolder's provider selection."""

import streamlit as st

from asklit.config import get_api_key, get_base_url
from asklit.experiments import parse_model_names
from asklit.llm import get_allowed_models
from asklit.models import choose_model_options, discover_available_models


@st.cache_data(ttl=300, show_spinner=False)
def discover_endpoint_models(provider, base_url):
    """Cache public model metadata without placing credentials in the cache key."""
    return discover_available_models(
        provider,
        base_url,
        get_api_key(provider),
    )


def get_endpoint_model_choices(provider, configured_models, base_url_override=None):
    """Choose honest runnable options without mistaking Azure's catalog for deployments."""
    base_url = (
        base_url_override if base_url_override is not None else get_base_url(provider)
    )
    discovery = discover_endpoint_models(provider, base_url)
    configured_models = parse_model_names(configured_models)

    choices, choice_source = choose_model_options(discovery, configured_models)
    return choices, choice_source, discovery


def host_runnable_models(model_choices=None):
    """List the models this scaffolder host may actually be asked to run.

    Passed to ``call_llm`` as ``extra_allowed_models`` so scaffolder experiments
    stay inside the operator's ``model.allowed_models`` ceiling when one is set,
    instead of disabling allowlist enforcement outright.
    """
    return parse_model_names(
        [
            *(model_choices or []),
            *get_allowed_models(),
        ]
    )


def render_endpoint_model_status(discovery, choice_source):
    host = discovery.get("endpoint_host") or "default endpoint"
    st.caption(f"Endpoint: {discovery['endpoint_label']} · {host}")
    count = len(discovery["models"])
    if discovery["is_azure"] and count:
        st.caption(
            f"Azure `/models` returned {count} catalog entries; these are not treated "
            f"as deployed models. Showing {choice_source or 'manual model entry'}."
        )
    elif choice_source:
        st.caption(f"Showing {choice_source}.")
    elif discovery["error"]:
        st.caption(f"Automatic model discovery unavailable: {discovery['error']}")
    elif count > 20:
        st.caption(
            f"The endpoint returned {count} models, so manual entry remains available."
        )
