"""Streamlit widgets for the owner-file and anomaly-rule editors. All logic (validation, merging, safe saving) is in editors.py."""
from __future__ import annotations
import pandas as pd
import streamlit as st

from . import editors as E
from . import tags as T
from .ui import money


def _reset_flag(key: str) -> None:
    """After a save the editor must start again from the file; Streamlit keeps edits in session state until the key is dropped."""
    if st.session_state.pop(f"{key}_reset", False):
        st.session_state.pop(key, None)
        st.session_state.pop(f"{key}_pf", None)


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

    no_code = full["project_code"].fillna("").str.strip().eq("")
    unspec = full["environment"].fillna("").str.strip().str.lower().isin(["", "unspecified"])
    options = {"Without an owner": full["owner"].eq(""), "Without a project code": no_code, "Environment unspecified": unspec, "All rows": pd.Series(True, index=full.index)}
    pick = st.radio("Show", [f"{k} ({int(v.sum())})" if k != "All rows" else k for k, v in options.items()], index=0 if len(blank) else 3, horizontal=True, key=f"{key}_show",
                    help="Rows that still need something. Fix them, save, and the list shrinks.")
    view = full[options[pick.split(" (")[0]]].sort_values("Last 12 mo", ascending=False)
    shown = {r for r in view[E.RID].dropna()}

    # cloud tags / labels: read-only reference columns beside what you type
    acc, vals = T.read_accounts(), T.read_values()
    dom = T.dominant(vals, acc)
    have_tags = T.has_tags(prov)
    cloud_keys, prefill = [], False
    if have_tags:
        pulled = sorted(dom[dom.provider == prov]["key"].unique(), key=lambda k: -float(dom[(dom.provider == prov) & (dom.key == k)]["tagged"].sum()))
        mapped = [k for f in T.FINANCE_FIELDS for k in pulled if k.lower() in {c.lower() for c in T.tag_config()["map"][f]}]
        default = mapped or pulled[:3]
        cloud_keys = st.multiselect("Cloud tags to show beside each row (read-only)", pulled, default=[k for k in default if k in pulled], key=f"{key}_tagkeys",
                                    help="Value with the most spend in the account / project. “+2” = more values exist; “· 13%” = only that share of the spend carries the tag.")
        prefill = st.checkbox("Pre-fill empty cost_center / project_code from the cloud tag", key=f"{key}_prefill",
                              help="Only EMPTY cells are filled, typed values are never replaced. Nothing is saved until you click Save owners. Switching this resets unsaved edits.")
    else:
        st.info(f"No cloud tags pulled for {prov} yet. Click **Refresh {prov} tags** at the top of this page to see the tags beside each row.")

    view = view.reset_index(drop=True)
    name_col = core[1] if prov == "AWS" else core[0]
    cloud = T.cloud_columns(prov, view, name_col, cloud_keys, dom, acc) if have_tags else pd.DataFrame(index=view.index)
    if have_tags:
        sugg = T.suggestions(prov, view, name_col, dom, acc)
        chk = T.check(view, sugg)
        if prefill:
            view = T.prefill(view, sugg)
        cloud["Tag check"] = chk
    show_df = pd.concat([view[core + ["Last 12 mo"]], cloud, view[[E.RID]]], axis=1)
    show_df["treatment"] = show_df["treatment"].replace("", None)
    cfg = {E.RID: None, "Last 12 mo": st.column_config.NumberColumn("Last 12 mo", format="$%,.0f", disabled=True, help="Spend over the last 12 billed months (all categories)"),
           "owner": st.column_config.TextColumn("owner", help="Use the same spelling every time: names are matched exactly"),
           name: st.column_config.TextColumn(name, required=True),
           "cost_center": st.column_config.TextColumn("cost_center", help="Finance cost center code"),
           "project_code": st.column_config.TextColumn("project_code", help="Finance project code"),
           "treatment": st.column_config.SelectboxColumn("treatment", options=E.TREATMENTS, help="Opex, Capex-eligible or Prepaid: a note for Finance, not used in any calculation yet"),
           "finance_notes": st.column_config.TextColumn("finance_notes")}
    for c in cloud.columns:
        cfg[c] = st.column_config.TextColumn(c, disabled=True, help="From the cloud, read-only. Refresh tags to update." if c != "Tag check" else "Compares what you typed with the cloud tag")
    ekey = f"{key}_pf" if prefill else key
    edited = st.data_editor(show_df, key=ekey, hide_index=True, num_rows="dynamic", column_config=cfg, width="stretch")

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
                for col in ("project_code", "environment"):
                    for rare, common in E.near_duplicate_owners(merged[col]):
                        st.warning(f"{col}: “{rare}” looks like “{common}”. Saved as typed: fix the spelling if they are the same thing (reports group by the exact text, ignoring case).")
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


# --------------------------------------------------------------------------------------- tag coverage and catalog
def tags_panel() -> None:
    """What the cloud knows: how much spend carries a finance tag, which accounts are missing it, and every tag key that was found."""
    acc, vals, cat = T.read_accounts(), T.read_values(), T.read_catalog()
    if acc.empty:
        st.info("No cloud tags pulled yet. Use **Refresh AWS tags** / **Refresh GCP tags** at the top of this page.")
        return
    dom = T.dominant(vals, acc)
    cfg = T.tag_config()
    tabs = st.tabs([p for p in ("AWS", "GCP") if (acc.provider == p).any()])
    for tab, prov in zip(tabs, [p for p in ("AWS", "GCP") if (acc.provider == p).any()]):
        with tab:
            a = acc[acc.provider == prov]
            st.caption(f"Window: {a['window'].iloc[0]} (billed months) · pulled {a['pulled_at'].iloc[0].replace('T', ' ')} · {len(a)} {'accounts' if prov == 'AWS' else 'projects'}, {money(a['total'].sum())} of spend.")
            cols = st.columns(len(T.FINANCE_FIELDS))
            for col, f in zip(cols, T.FINANCE_FIELDS):
                c = T.coverage(prov, f, dom, acc, cfg)
                col.metric(f"{f} tagged", f"{c['share']:.0%} of spend", f"{c['with_tag']} of {c['accounts']} have it", delta_color="off",
                           help=("Tag keys looked at: " + ", ".join(c["keys"])) if c["keys"] else
                           "None of the mapped tag keys (" + ", ".join(cfg["map"][f]) + ") was pulled. In AWS the tag must be ACTIVE in Billing > Cost allocation tags.")
                with col.expander(f"Biggest spend without {f}"):
                    st.dataframe(c["untagged"].head(15).style.format({"Spend without it": money}), hide_index=True, width="stretch")
            c1 = cat[cat.provider == prov].copy()
            c1["rank"] = c1["pulled"].map({True: 0, False: 1})
            c1 = c1.sort_values(["rank", "tagged_cost"], ascending=[True, False]).drop(columns="rank")
            only = st.checkbox("Only keys that were pulled", value=True, key=f"tagcat_{prov}")
            st.markdown(f"**Every tag key found ({len(c1)})**")
            st.dataframe((c1[c1.pulled] if only else c1).rename(columns={"key": "Tag key", "status": "Status", "pulled": "Pulled", "accounts": "Accounts", "values": "Values",
                                                                          "tagged_cost": "Spend tagged", "note": "Note"}).drop(columns="provider"),
                         hide_index=True, width="stretch",
                         column_config={"Spend tagged": st.column_config.NumberColumn(format="$%,.0f")})
            st.caption("Tag keys are chosen in `config.toml` under `[tags]`. Which key feeds cost_center / project_code is set in `[tags.map]`.")
