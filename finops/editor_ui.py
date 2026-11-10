"""Streamlit widgets for the owner-file and anomaly-rule editors. All logic (validation, merging, safe saving) is in editors.py."""
from __future__ import annotations
import pandas as pd
import streamlit as st

from . import editors as E
from .ui import money


def _reset_flag(key: str) -> None:
    """After a save the editor must start again from the file; Streamlit keeps edits in session state until the key is dropped."""
    if st.session_state.pop(f"{key}_reset", False):
        st.session_state.pop(key, None)


def _saved(key: str, path, backup, msg: str) -> None:
    st.cache_data.clear()                                   # owners / rules are applied at read time
    st.session_state[f"{key}_mtime"] = E.mtime(path)
    st.session_state[f"{key}_reset"] = True
    st.session_state["_editor_flash"] = msg + (f" Previous version kept in `{backup.parent.name}/{backup.name}`." if backup else "")
    st.rerun()


def _flash() -> None:
    m = st.session_state.pop("_editor_flash", None)
    if m:
        st.success(m)


def _loaded_mtime(key: str, path) -> float:
    k = f"{key}_mtime"
    if k not in st.session_state:
        st.session_state[k] = E.mtime(path)
    return st.session_state[k]


# --------------------------------------------------------------------------------------- owners
def owners_editor(df_all: pd.DataFrame) -> None:
    """Assign owners to accounts / projects. df_all: billing rows (any category), used for spend and to find accounts missing from the file."""
    st.caption("Owners decide who gets the savings and increase questions. Fill the blanks and click **Save owners**: the file "
               "(`data/owners_*.csv`) is backed up first and the dashboard picks the change up immediately. Columns this editor does not show are kept as they are.")
    _flash()
    prov = st.radio("Provider", ["AWS", "GCP"], horizontal=True, key="own_prov")
    key = f"own_{prov}"
    _reset_flag(key)
    path = E.owner_path(prov)
    loaded = _loaded_mtime(key, path)
    table = E.read_owner_file(prov)
    spend = E.spend_by_account(df_all, prov)
    core, name = E.OWNER_CORE[prov], E.OWNER_KEY[prov]
    missing = E.missing_accounts(table, spend, prov)
    full = pd.concat([table, missing], ignore_index=True)
    full["Last 12 mo"] = [E.row_spend(table, spend, prov).iloc[i] if i < len(table) else float(spend.get(n, 0.0)) for i, n in enumerate(full[name])]

    blank = full[(full["owner"] == "")]
    k = st.columns(4)
    k[0].metric("Rows in the file", len(table))
    k[1].metric("Without an owner", len(blank), help="Rows with a blank owner, plus accounts that bill but are not in the file at all")
    k[2].metric("Spend with no owner (12 mo)", money(blank["Last 12 mo"].sum()))
    k[3].metric("Billing but not in the file", len(missing), help="They are added to the table below with a blank owner. Give them an owner (or leave the row blank to skip it).")

    only_blank = st.checkbox("Only rows without an owner", value=len(blank) > 0, key=f"{key}_only")
    view = (blank if only_blank else full).sort_values("Last 12 mo", ascending=False)
    shown = {r for r in view[E.RID].dropna()}
    cols = core + ["Last 12 mo", E.RID]
    cfg = {E.RID: None, "Last 12 mo": st.column_config.NumberColumn("Last 12 mo", format="$%,.0f", disabled=True, help="Spend over the last 12 billed months (all categories)"),
           "owner": st.column_config.TextColumn("owner", help="Use the same spelling every time: names are matched exactly"),
           name: st.column_config.TextColumn(name, required=True)}
    edited = st.data_editor(view[cols].reset_index(drop=True), key=key, hide_index=True, num_rows="dynamic", column_config=cfg, width="stretch")

    c1, c2 = st.columns([1, 4])
    if c2.button("Discard edits / reload from file", key=f"{key}_reload"):
        st.session_state[f"{key}_mtime"] = E.mtime(path)
        st.session_state[f"{key}_reset"] = True
        st.rerun()
    if c1.button("Save owners", type="primary", key=f"{key}_save"):
        orig_full = pd.concat([table, missing], ignore_index=True)
        merged, stats = E.merge_owner_edits(orig_full, edited, shown, prov)
        # accounts that were only proposed (not in the file) and still have no owner are not written
        errors, warns = E.validate_owners(prov, merged)
        if errors:
            for e in errors:
                st.error(e)
        elif stats["changed"] + stats["added"] + stats["deleted"] == 0:
            st.info("Nothing to save: no changes.")
        else:
            try:
                E.check_fresh(path, loaded)
            except E.Stale as ex:
                st.error(str(ex))
            else:
                for w in warns:
                    st.warning(w)
                for rare, common in E.near_duplicate_owners(merged["owner"]):
                    st.warning(f"“{rare}” looks like “{common}”. Saved as typed: fix the spelling if they are the same person.")
                b = E.save_csv(path, merged)
                _saved(key, path, b, f"Saved {path.name}: {stats['changed']} changed, {stats['added']} added, {stats['deleted']} removed.")


# --------------------------------------------------------------------------------------- anomaly rules
def rules_editor(anomalies: pd.DataFrame | None) -> None:
    """Record reviews of detected billing anomalies; saving rebuilds the forecast with them."""
    st.caption("A **review** says what a detected anomaly is. `add_back` = billing timing: the missing months are filled with the line's typical value "
               "(trend only). `confirmed_real` = a real drop, left as is. `pending` = not reviewed yet. Saving writes `data/anomaly_rules.csv` "
               "(backed up first) and the forecast rebuilds with it.")
    _flash()
    path = E.rules_path()
    key = "rules"
    _reset_flag(key)
    loaded = _loaded_mtime(key, path)
    rules = E.read_rules()

    sug = E.suggest_rules(anomalies, rules)
    if len(sug):
        st.markdown(f"**Detected, not in the file yet ({len(sug)})** — tick the ones to review, choose what they are, add a note:")
        cfg = {"Add": st.column_config.CheckboxColumn("Add"), "action": st.column_config.SelectboxColumn("review", options=E.RULE_ACTIONS, required=True),
               "Missing vs typical": st.column_config.NumberColumn(format="$%,.0f", disabled=True), "Type": st.column_config.TextColumn(disabled=True),
               "provider": st.column_config.TextColumn(disabled=True), "account": st.column_config.TextColumn(disabled=True),
               "service": st.column_config.TextColumn(disabled=True), "months": st.column_config.TextColumn(disabled=True)}
        ed = st.data_editor(sug, key="rules_sug", hide_index=True, column_config=cfg, width="stretch")
        if st.button("Add ticked rows to the rules", key="rules_add"):
            pick = ed[ed["Add"]]
            if pick.empty:
                st.info("Tick at least one row.")
            else:
                new = pd.concat([rules[E.RULE_COLS], pick[E.RULE_COLS]], ignore_index=True)
                errors, warns = E.validate_rules(new)
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    try:
                        E.check_fresh(path, loaded)
                    except E.Stale as ex:
                        st.error(str(ex))
                    else:
                        _saved("rules", path, E.save_csv(path, E.merge_rule_edits(rules, new)[0]), f"Added {len(pick)} review(s) to {path.name}.")
    else:
        st.success("Every detected anomaly already has a row in the rules file.")

    st.markdown(f"**Reviews on file ({len(rules)})** — edit in place, add a row with the + at the bottom, or select a row and press Delete:")
    cfg = {E.RID: None,
           "provider": st.column_config.SelectboxColumn("provider", options=["AWS", "GCP"], required=True),
           "action": st.column_config.SelectboxColumn("review", options=E.RULE_ACTIONS, required=True),
           "months": st.column_config.TextColumn("months", help="Gap months as YYYY-MM separated by ;  e.g. 2026-02;2026-03. Needed for add_back."),
           "account": st.column_config.TextColumn("account / project", required=True), "service": st.column_config.TextColumn("service", required=True),
           "note": st.column_config.TextColumn("note", help="Who confirmed it and why")}
    edited = st.data_editor(rules[E.RULE_COLS + [E.RID]], key="rules_tbl", hide_index=True, num_rows="dynamic", column_config=cfg, width="stretch")
    if st.button("Save reviews", type="primary", key="rules_save"):
        merged, stats = E.merge_rule_edits(rules, edited)
        errors, warns = E.validate_rules(merged)
        if errors:
            for e in errors:
                st.error(e)
        elif not stats["changed"]:
            st.info("Nothing to save: no changes.")
        else:
            try:
                E.check_fresh(path, loaded)
            except E.Stale as ex:
                st.error(str(ex))
            else:
                for w in warns:
                    st.warning(w)
                _saved("rules", path, E.save_csv(path, merged), f"Saved {path.name}: {stats['rows']} review(s).")
