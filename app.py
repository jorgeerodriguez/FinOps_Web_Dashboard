import streamlit as st

st.set_page_config(page_title="FinOps Dashboard", page_icon="💲", layout="wide")

pages = {
    "Overview": [
        st.Page("views/executive.py", title="Executive Summary", icon="📊", default=True),
        st.Page("views/history.py", title="Two-Year History", icon="🕰️"),
        st.Page("views/as_now.py", title="Month to Date", icon="⏱️"),
        st.Page("views/compare.py", title="AWS vs GCP", icon="⚖️"),
        st.Page("views/relationships.py", title="Relationships", icon="🕸️"),
    ],
    "Providers": [
        st.Page("views/aws.py", title="AWS", icon="🟠"),
        st.Page("views/gcp.py", title="GCP", icon="🔵"),
        st.Page("views/marketplace.py", title="Marketplace", icon="🛒"),
    ],
    "Other platforms": [
        st.Page("views/equinix.py", title="Equinix", icon="🔌"),
        st.Page("views/grafana.py", title="Grafana Cloud", icon="📈"),
    ],
    "Planning": [
        st.Page("views/forecast.py", title="Forecast & Budget", icon="🔮"),
        st.Page("views/next_year.py", title="Next Year Forecast", icon="📅"),
        st.Page("views/insights.py", title="Insights & Savings", icon="💡"),
        st.Page("views/orphans.py", title="Orphaned Accounts & Projects", icon="👻"),
    ],
    "System": [
        st.Page("views/data_health.py", title="Data Health", icon="🩺"),
    ],
}
from finops.ui import view_picker

nav = st.navigation(pages)
view_picker()
nav.run()
