#!/usr/bin/env python3
"""
Auswertung UF1-B / UF2 / UF3 (GitOps mit Argo CD vs. Push-Referenz).

Eingaben (benchmark-suite/results/):
    natural_selfheal_measurements.csv      (UF1-B, Skript run_natural_selfheal.py)
    mttr_measurements_v2.csv               (UF2,   Skript run_rollback_mttr.py)
    disaster_recovery_measurements_v2.csv  (UF3,   Skript run_disaster_recovery.py)

Grundsätze:
- Nur Läufe mit valid == 1 werden ausgewertet; Ausschlüsse werden gezählt und begründet ausgegeben.
- Keine Strafwerte: Zeiten werden nur über erfolgreiche Läufe berichtet, Raten getrennt.
- Keine Tests auf simulierte Werte. H1a wird als analytisches Modell (Push-Wartezeit ~ U(0,T)) dargestellt.
- Mann-Whitney-U: Methode wird je Test ausgewiesen (exakt ohne Bindungen, sonst asymptotisch).
- Cliff's Delta: delta = P(GitOps > Push) - P(GitOps < Push); positiv = GitOps-Werte größer (langsamer).
- Mehrfachtests: Holm-Korrektur je Testfamilie (Rohwert, Rang, korrigierter Wert).
- Keine hartkodierten Ergebnis- oder Bewertungszahlen.

Aufruf:
    python evaluate_stats.py
    python evaluate_stats.py --results-dir ../results
"""

import argparse
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import scipy  # noqa: E402
from scipy import stats  # noqa: E402

# ==============================================================================
# KONFIGURATION
# ==============================================================================
ALPHA = 0.05
MIN_N_TEST = 3  # Mindestanzahl erfolgreicher Läufe je Gruppe für einen Test
SEED = 42
N_BOOT = 5000
T_VALUES = [
    5,
    10,
    15,
    30,
    60,
    120,
    300,
]  # Push-Intervalle T (s) für die Sensitivität (Modell)

G_UF1 = "GitOps_ArgoCD_Natural"
G_UF23 = "GitOps_ArgoCD"
G_POLL = "GitOps_ArgoCD_Polling"
PUSH = "Push_Pipeline"

LABEL = {
    G_UF1: "Argo CD (unberührt)",
    G_UF23: "Argo CD",
    G_POLL: "Argo CD (Polling)",
    PUSH: "Push-Referenz",
}
COLOR = {G_UF1: "#1b9e77", G_UF23: "#1b9e77", G_POLL: "#7570b3", PUSH: "#d95f02"}

SCRIPT_DIR = Path(__file__).resolve().parent


class Report:
    def __init__(self):
        self.lines = []

    def p(self, text=""):
        print(text)
        self.lines.append(str(text))

    def h(self, title):
        self.p("\n" + "=" * 78)
        self.p(title)
        self.p("=" * 78)


R = Report()
TESTS = []  # gesammelte Hypothesentests (Holm wird am Ende je Familie angewandt)


# ==============================================================================
# STATISTIK-HELFER
# ==============================================================================
def fmt_p(p):
    if p is None or (isinstance(p, float) and np.isnan(p)):
        return "n/a"
    return f"{p:.2e}" if p < 1e-3 else f"{p:.4f}"


def describe(s):
    s = pd.to_numeric(pd.Series(s), errors="coerce").dropna()
    if len(s) == 0:
        return dict(n=0, mean=np.nan, sd=np.nan, median=np.nan, min=np.nan, max=np.nan)
    return dict(
        n=len(s),
        mean=s.mean(),
        sd=s.std(ddof=1) if len(s) > 1 else np.nan,
        median=s.median(),
        min=s.min(),
        max=s.max(),
    )


def describe_table(groups, digits=3):
    """groups: dict label -> Series. Gibt eine formatierte Tabelle zurück."""
    rows = []
    for label, s in groups.items():
        d = describe(s)
        rows.append(
            {
                "Gruppe": label,
                **{k: (round(v, digits) if k != "n" else int(v)) for k, v in d.items()},
            }
        )
    return pd.DataFrame(rows).to_string(index=False)


def cliffs_delta(x, y):
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) == 0 or len(y) == 0:
        return np.nan, "keine Daten"
    greater = (x[:, None] > y[None, :]).sum()
    less = (x[:, None] < y[None, :]).sum()
    d = (greater - less) / (len(x) * len(y))
    a = abs(d)
    if a < 0.147:
        eff = "vernachlässigbar"
    elif a < 0.33:
        eff = "klein"
    elif a < 0.474:
        eff = "mittel"
    else:
        eff = "groß"
    return float(d), eff


def mwu(x, y):
    """Zweiseitiger Mann-Whitney-U-Test; exakt ohne Bindungen (n <= 50), sonst asymptotisch."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    ties = len(np.unique(np.concatenate([x, y]))) < len(x) + len(y)
    method = "asymptotisch" if (ties or min(len(x), len(y)) > 50) else "exakt"
    res = stats.mannwhitneyu(
        x,
        y,
        alternative="two-sided",
        method="asymptotic" if method == "asymptotisch" else "exact",
    )
    return float(res.statistic), float(res.pvalue), method


def bootstrap_median_diff(x, y, n_boot=N_BOOT, seed=SEED):
    """Median(x) - Median(y) mit Perzentil-Bootstrap-95%-KI."""
    rng = np.random.default_rng(seed)
    x, y = np.asarray(x, float), np.asarray(y, float)
    diff = float(np.median(x) - np.median(y))
    bx = rng.choice(x, size=(n_boot, len(x)), replace=True)
    by = rng.choice(y, size=(n_boot, len(y)), replace=True)
    d = np.median(bx, axis=1) - np.median(by, axis=1)
    lo, hi = np.percentile(d, [2.5, 97.5])
    return diff, float(lo), float(hi)


def holm_adjust(pvals):
    p = np.asarray(pvals, float)
    m = len(p)
    order = np.argsort(p)
    adj = np.empty(m)
    running = 0.0
    for rank, i in enumerate(order):
        running = max(running, (m - rank) * p[i])
        adj[i] = min(1.0, running)
    return adj


def add_time_test(family, name, g, p_vals, g_label="GitOps", p_label="Push"):
    """Mann-Whitney + Cliff's Delta + Median-Differenz; wird für die Holm-Korrektur vorgemerkt."""
    g = pd.to_numeric(pd.Series(g), errors="coerce").dropna().to_numpy()
    p_vals = pd.to_numeric(pd.Series(p_vals), errors="coerce").dropna().to_numpy()
    if len(g) < MIN_N_TEST or len(p_vals) < MIN_N_TEST:
        R.p(
            f"  [Test übersprungen] {name}: n_{g_label}={len(g)}, n_{p_label}={len(p_vals)} (Minimum {MIN_N_TEST})"
        )
        return
    U, p, method = mwu(g, p_vals)
    delta, eff = cliffs_delta(g, p_vals)
    diff, lo, hi = bootstrap_median_diff(g, p_vals)
    TESTS.append(
        dict(
            family=family,
            test=name,
            kind="Mann-Whitney-U",
            n_gitops=len(g),
            n_push=len(p_vals),
            statistic=U,
            method=method,
            p_raw=p,
            effect=delta,
            effect_label=eff,
            median_diff_s=diff,
            ci_lo=lo,
            ci_hi=hi,
        )
    )


def add_fisher_test(family, name, k_g, n_g, k_p, n_p):
    if n_g == 0 or n_p == 0:
        return
    table = [[k_g, n_g - k_g], [k_p, n_p - k_p]]
    odds, p = stats.fisher_exact(table, alternative="two-sided")
    TESTS.append(
        dict(
            family=family,
            test=name,
            kind="Fisher exakt",
            n_gitops=n_g,
            n_push=n_p,
            statistic=k_g,
            method="exakt",
            p_raw=float(p),
            effect=(k_g / n_g) - (k_p / n_p),
            effect_label="Differenz der Raten",
            median_diff_s=np.nan,
            ci_lo=np.nan,
            ci_hi=np.nan,
        )
    )


def finalize_tests(out_dir):
    R.h("HYPOTHESENTESTS MIT HOLM-KORREKTUR (je Testfamilie)")
    if not TESTS:
        R.p("Keine Tests durchgeführt.")
        return
    df = pd.DataFrame(TESTS)
    parts = []
    for fam, sub in df.groupby("family", sort=False):
        sub = sub.copy()
        sub["p_holm"] = holm_adjust(sub["p_raw"].to_numpy())
        sub["rank"] = sub["p_raw"].rank(method="first").astype(int)
        sub["significant_holm"] = sub["p_holm"] < ALPHA
        parts.append(sub)
    df = pd.concat(parts)
    for fam, sub in df.groupby("family", sort=False):
        R.p(f"\nFamilie: {fam}  (m = {len(sub)} Tests, alpha = {ALPHA})")
        view = sub.sort_values("rank")[
            [
                "rank",
                "test",
                "kind",
                "n_gitops",
                "n_push",
                "method",
                "p_raw",
                "p_holm",
                "significant_holm",
                "effect",
                "effect_label",
                "median_diff_s",
                "ci_lo",
                "ci_hi",
            ]
        ].copy()
        view["p_raw"] = view["p_raw"].map(fmt_p)
        view["p_holm"] = view["p_holm"].map(fmt_p)
        for c in ("effect", "median_diff_s", "ci_lo", "ci_hi"):
            view[c] = view[c].round(3)
        R.p(view.to_string(index=False))
    R.p(
        "\nLesehinweise: Cliff's Delta = P(GitOps > Push) - P(GitOps < Push); positiv = GitOps-Werte größer. "
        "Median-Differenz = Median(GitOps) - Median(Push) in Sekunden mit Bootstrap-95%-KI "
        f"({N_BOOT} Ziehungen, Seed {SEED}). Bei Fisher-Tests ist 'effect' die Differenz der Erfolgsraten. "
        "Ein nicht signifikanter Test belegt keine Gleichheit."
    )
    df.to_csv(out_dir / "hypothesis_tests.csv", index=False)


# ==============================================================================
# EINLESEN & VALIDITÄT
# ==============================================================================
def load(path, required, name):
    if not path.exists():
        R.p(f"[!] {name}: Datei nicht gefunden ({path}). Modul übersprungen.")
        return None
    df = pd.read_csv(path)
    missing = [c for c in required if c not in df.columns]
    if missing:
        R.p(f"[!] {name}: fehlende Spalten {missing}. Modul übersprungen.")
        return None
    df["valid"] = pd.to_numeric(df["valid"], errors="coerce").fillna(0).astype(int)
    return df


def validity_report(df, name):
    R.p(f"\nGültigkeit der Läufe ({name}):")
    g = df.groupby("model")["valid"].agg(gesamt="count", gueltig="sum")
    g["ausgeschlossen"] = g["gesamt"] - g["gueltig"]
    R.p(g.to_string())
    excl = df[df["valid"] == 0]
    if len(excl) and "note" in df.columns:
        R.p("Gründe der Ausschlüsse (note):")
        R.p(excl["note"].fillna("(ohne Angabe)").value_counts().to_string())
    return df[df["valid"] == 1].copy()


def pattern_key(p):
    return int(str(p)[1:])


def spearman_diag(x, y):
    x, y = pd.Series(x).astype(float), pd.Series(y).astype(float)
    mask = x.notna() & y.notna()
    if mask.sum() < 4:
        return np.nan, np.nan, int(mask.sum())
    rho, p = stats.spearmanr(x[mask], y[mask])
    return float(rho), float(p), int(mask.sum())


# ==============================================================================
# UF1-B
# ==============================================================================
def evaluate_uf1(path, out_dir, plots):
    req = [
        "model",
        "pattern",
        "run",
        "valid",
        "recovered",
        "remediation_time_s",
        "ts_utc",
    ]
    df = load(path, req, "UF1-B")
    if df is None:
        return None
    R.h(
        "UF1-B: DRIFT-BEHEBUNG (Zeit von Injektionsende bis Verifikation des Soll-Zustands)"
    )
    R.p(
        "Gemessene Größe: Zeit bis zur Behebung (Erkennung + Heilung zusammen). Es gibt keine getrennte "
        "Erkennungszeit. Argo CD läuft ohne Eingriff im Messfenster; Push führt je Lauf genau einen "
        "'kubectl apply' aus (Prozessstart in der Zeit enthalten)."
    )
    for c in ("recovered", "remediation_time_s"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    v = validity_report(df, "UF1-B")

    models_present = set(v["model"].unique())
    if G_UF1 not in models_present or PUSH not in models_present:
        R.p(
            f"[!] Erwartet werden die Modelle {G_UF1} und {PUSH}; vorhanden: {sorted(models_present)}"
        )
        return None

    if "reset_counter" in v.columns:
        R.p(
            f"\nZähler-Reset zwischen Läufen (Spalte reset_counter): {sorted(v['reset_counter'].dropna().unique().tolist())}"
        )
    ts = pd.to_datetime(v["ts_utc"], errors="coerce")
    blockweise = ts[v["model"] == G_UF1].max() < ts[v["model"] == PUSH].min()
    R.p(f"Messreihenfolge blockweise (GitOps vollständig vor Push): {blockweise}")

    patterns = sorted(v["pattern"].unique(), key=pattern_key)
    rows = []
    time_family = "UF1 Zeit bis Behebung je Muster (nur erfolgreiche Läufe)"
    rate_family = "UF1 Behebungsrate je Muster (Fisher)"

    for pat in patterns:
        sub = v[v["pattern"] == pat]
        g = sub[sub["model"] == G_UF1]
        pu = sub[sub["model"] == PUSH]
        g_ok = g[g["recovered"] == 1]["remediation_time_s"]
        p_ok = pu[pu["recovered"] == 1]["remediation_time_s"]
        dg, dp = describe(g_ok), describe(p_ok)
        rows.append(
            {
                "Muster": pat,
                "Argo_geheilt": f"{int(g['recovered'].sum())}/{len(g)}",
                "Argo_Mittel_s": round(dg["mean"], 3),
                "Argo_SD_s": round(dg["sd"], 3),
                "Argo_Median_s": round(dg["median"], 3),
                "Push_geheilt": f"{int(pu['recovered'].sum())}/{len(pu)}",
                "Push_Mittel_s": round(dp["mean"], 3),
                "Push_SD_s": round(dp["sd"], 3),
                "Push_Median_s": round(dp["median"], 3),
            }
        )
        add_time_test(
            time_family, f"{pat}: Zeit bis Behebung", g_ok, p_ok, "Argo", "Push"
        )
        add_fisher_test(
            rate_family,
            f"{pat}: Behebungsrate",
            int(g["recovered"].sum()),
            len(g),
            int(pu["recovered"].sum()),
            len(pu),
        )

    tab = pd.DataFrame(rows)
    R.p(
        "\nAuswertung je Drift-Muster (Zeiten nur über erfolgreich behobene Läufe; '-' = keine):"
    )
    R.p(tab.fillna("-").to_string(index=False))
    tab.to_csv(out_dir / "uf1_pattern_table.csv", index=False)

    # Behebungsraten gesamt
    R.p("\nDrift Recovery Rate (DRR) über alle gültigen Läufe:")

    def rate(df_, excl=()):
        d = df_[~df_["pattern"].isin(excl)]
        out = {}
        for m, lab in ((G_UF1, "Argo CD"), (PUSH, "Push")):
            s = d[d["model"] == m]["recovered"]
            out[lab] = (
                f"{int(s.sum())}/{len(s)} = {100 * s.mean():.1f} %" if len(s) else "n/a"
            )
        return out

    R.p(f"  alle Muster:              {rate(v)}")
    R.p(f"  ohne D8:                  {rate(v, ('D8',))}")
    R.p(f"  ohne D8 und D11:          {rate(v, ('D8', 'D11'))}")
    R.p(
        "  Hinweis: Der Unterschied in der Gesamtrate wird von einzelnen Mustern (D8; D11 bei beiden) getragen. "
        "D8 wird von Argo CD nur wegen des manuell gesetzten Tracking-Kontexts bereinigt."
    )

    # Verhalten bei nicht behobenen Läufen
    if "post_window_sync_status" in v.columns:
        nr = v[v["recovered"] == 0]
        R.p(
            "\nNicht behobene Läufe: Argo-Sync-Status nach dem Messfenster (Push = N/A):"
        )
        if len(nr):
            R.p(
                pd.crosstab(
                    [nr["pattern"], nr["model"]],
                    nr["post_window_sync_status"].fillna("?"),
                ).to_string()
            )
        else:
            R.p("  keine")

    # Gepoolte Deskription (nur deskriptiv, Daten sind nach Muster gruppiert)
    ok = v[v["recovered"] == 1]
    gp = ok[ok["model"] == G_UF1]["remediation_time_s"]
    pp = ok[ok["model"] == PUSH]["remediation_time_s"]
    R.p(
        "\nGepoolte Zeiten aller erfolgreich behobenen Läufe (nur deskriptiv; Messpunkte sind nach Muster "
        "gruppiert und nicht unabhängig, daher kein p-Wert):"
    )
    R.p(describe_table({"Argo CD": gp, "Push": pp}))
    d, e = cliffs_delta(gp, pp)
    R.p(f"  Cliff's Delta (gepoolt, deskriptiv): {d:.3f} ({e})")

    # Diagnose: Heal-Zähler und Reihenfolge
    g_ok_df = ok[ok["model"] == G_UF1].copy()
    if "heal_attempts_before" in g_ok_df.columns:
        cnt = pd.to_numeric(g_ok_df["heal_attempts_before"], errors="coerce")
        rho, p, n = spearman_diag(cnt, g_ok_df["remediation_time_s"])
        R.p(
            f"\nDiagnose Backoff-Zähler: Spearman(heal_attempts_before, Zeit) = {rho:.3f} (p = {fmt_p(p)}, n = {n}); "
            f"Zählerbereich {cnt.min()} bis {cnt.max()}. Ein Zusammenhang würde auf Restwirkung des Backoffs hindeuten."
        )
    for m, lab in ((G_UF1, "Argo CD"), (PUSH, "Push")):
        sub = ok[ok["model"] == m].copy()
        sub["ts"] = pd.to_datetime(sub["ts_utc"], errors="coerce")
        sub = sub.sort_values("ts")
        sub["resid"] = sub["remediation_time_s"] - sub.groupby("pattern")[
            "remediation_time_s"
        ].transform("median")
        sub["seq"] = np.arange(len(sub))
        rho, p, n = spearman_diag(sub["seq"], sub["resid"])
        R.p(
            f"Diagnose Zeittrend {lab}: Spearman(Laufreihenfolge, Abweichung vom Mustermedian) = {rho:.3f} (p = {fmt_p(p)}, n = {n})"
        )

    # Grafik 1: Boxplots je Muster
    fig, ax = plt.subplots(figsize=(13, 5.5))
    xt, xl = [], []
    rng = np.random.default_rng(SEED)
    for i, pat in enumerate(patterns):
        for off, m in ((-0.2, G_UF1), (0.2, PUSH)):
            vals = (
                v[(v["pattern"] == pat) & (v["model"] == m) & (v["recovered"] == 1)][
                    "remediation_time_s"
                ]
                .dropna()
                .to_numpy()
            )
            if len(vals):
                ax.boxplot(
                    vals,
                    positions=[i + off],
                    widths=0.32,
                    showfliers=False,
                    patch_artist=True,
                    boxprops=dict(facecolor=COLOR[m], alpha=0.45),
                    medianprops=dict(color="black"),
                )
                ax.scatter(
                    np.full(len(vals), i + off) + rng.uniform(-0.06, 0.06, len(vals)),
                    vals,
                    s=12,
                    color=COLOR[m],
                    edgecolor="black",
                    linewidth=0.3,
                    zorder=3,
                )
        sub = v[v["pattern"] == pat]
        kg = int(sub[sub["model"] == G_UF1]["recovered"].sum())
        ng = int((sub["model"] == G_UF1).sum())
        kp = int(sub[sub["model"] == PUSH]["recovered"].sum())
        np_ = int((sub["model"] == PUSH).sum())
        xt.append(i)
        xl.append(f"{pat}\nA {kg}/{ng}\nP {kp}/{np_}")
    ax.set_xticks(xt)
    ax.set_xticklabels(xl, fontsize=8)
    ax.set_ylabel("Zeit bis Behebung (s), nur erfolgreiche Läufe")
    ax.set_title(
        "UF1: Zeit bis zur Behebung je Drift-Muster (A = Argo CD, P = Push; Zahlen = behoben/gültig)"
    )
    ax.legend(
        handles=[
            plt.Rectangle((0, 0), 1, 1, color=COLOR[G_UF1], alpha=0.6),
            plt.Rectangle((0, 0), 1, 1, color=COLOR[PUSH], alpha=0.6),
        ],
        labels=[LABEL[G_UF1], LABEL[PUSH]],
        loc="upper left",
    )
    plt.tight_layout()
    plt.savefig(plots / "fig1_uf1_remediation_by_pattern.png", dpi=300)
    plt.close()

    return dict(
        argo_mean=float(gp.mean()) if len(gp) else np.nan,
        argo_min=float(gp.min()) if len(gp) else np.nan,
        argo_max=float(gp.max()) if len(gp) else np.nan,
        push_apply_mean=float(pp.mean()) if len(pp) else np.nan,
    )


# ==============================================================================
# H1a: ANALYTISCHES MODELL (kein Hypothesentest)
# ==============================================================================
def evaluate_h1a_model(uf1, plots):
    if not uf1 or np.isnan(uf1["argo_mean"]) or np.isnan(uf1["push_apply_mean"]):
        return
    R.h(
        "H1a: MODELL DER PUSH-PIPELINE MIT PERIODISCHEM AUSFÜHRUNGSINTERVALL T (analytisch)"
    )
    R.p(
        "Annahme: Ein Drift tritt zu einem zufälligen Zeitpunkt auf; die Wartezeit bis zum nächsten periodischen "
        "Pipeline-Lauf ist gleichverteilt U(0, T) mit E = T/2 und 95. Perzentil 0,95*T. Die Behebung selbst dauert so "
        "lange wie die gemessene Push-Apply-Zeit. Es handelt sich um ein Modell, nicht um Messdaten; "
        "es wird kein Signifikanztest gerechnet."
    )
    R.p(
        f"Gemessen: Argo CD Mittel {uf1['argo_mean']:.3f} s (Spanne {uf1['argo_min']:.3f} bis {uf1['argo_max']:.3f} s); "
        f"Push-Apply Mittel {uf1['push_apply_mean']:.3f} s (nur erfolgreich behobene Läufe, D8/D11 nicht behoben)."
    )
    rows = []
    for T in T_VALUES:
        rows.append(
            {
                "T_s": T,
                "E_Wartezeit_s": T / 2,
                "P95_Wartezeit_s": 0.95 * T,
                "E_Push_gesamt_s": T / 2 + uf1["push_apply_mean"],
                "Faktor_zu_Argo": round(
                    (T / 2 + uf1["push_apply_mean"]) / uf1["argo_mean"], 1
                ),
            }
        )
    R.p(pd.DataFrame(rows).to_string(index=False))
    R.p(
        "Gültigkeitsbedingung für die Argo-Werte: unberührtes Self-Healing mit deaktiviertem exponentiellem Backoff; "
        "Pause zwischen Drifts oberhalb der konfigurierten Self-Heal-Mindestpause. Der ereignisgesteuerte Push "
        "erkennt Drift ohne Commit gar nicht (Erkennungsrate 0 %, architekturbedingt, nicht gemessen)."
    )

    fig, ax = plt.subplots(figsize=(9, 5))
    Ts = np.array(T_VALUES, float)
    ax.plot(
        Ts,
        Ts / 2 + uf1["push_apply_mean"],
        marker="o",
        color=COLOR[PUSH],
        linewidth=2.2,
        label="Push mit Intervall T (Modell: T/2 + Apply-Zeit)",
    )
    ax.axhline(
        uf1["argo_mean"],
        color=COLOR[G_UF1],
        linewidth=2.2,
        label=f"Argo CD (gemessen, Mittel {uf1['argo_mean']:.2f} s)",
    )
    ax.axhspan(
        uf1["argo_min"],
        uf1["argo_max"],
        color=COLOR[G_UF1],
        alpha=0.12,
        label="Argo CD Spanne (gemessen)",
    )
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xticks(T_VALUES)
    ax.get_xaxis().set_major_formatter(plt.ScalarFormatter())
    ax.set_xlabel("Ausführungsintervall T der Push-Pipeline (s, log)")
    ax.set_ylabel("Erwartete Zeit bis Behebung (s, log)")
    ax.set_title("H1a (Modell): Zeit bis zur Behebung in Abhängigkeit von T")
    ax.legend()
    plt.tight_layout()
    plt.savefig(plots / "fig2_h1a_model_sensitivity.png", dpi=300)
    plt.close()


# ==============================================================================
# UF2
# ==============================================================================
def evaluate_uf2(path, plots):
    req = ["model", "run", "valid", "mttr_seconds"]
    df = load(path, req, "UF2")
    if df is None:
        return
    R.h("UF2: RELEASE-ROLLBACK / MTTR")
    R.p(
        "Messung: Zeit vom ersten Rollback-Befehl bis Frontend wieder mit gutem Image und 2/2 Replikaten ready. "
        "Fehlrelease wird in beiden Modellen per Commit eingespielt; Reihenfolge der Modelle wechselt je Lauf."
    )
    df["mttr_seconds"] = pd.to_numeric(df["mttr_seconds"], errors="coerce")
    v = validity_report(df, "UF2")
    fam = "UF2/UF3 Hypothesentests (H2, H3b)"

    g = v[v["model"] == G_UF23]["mttr_seconds"]
    p = v[v["model"] == PUSH]["mttr_seconds"]
    groups = {LABEL[G_UF23]: g, LABEL[PUSH]: p}
    if (v["model"] == G_POLL).any():
        groups[LABEL[G_POLL]] = v[v["model"] == G_POLL]["mttr_seconds"]
    R.p("\nMTTR in Sekunden (gültige Läufe):")
    R.p(describe_table(groups))
    add_time_test(fam, "UF2: MTTR (H2)", g, p, "Argo", "Push")
    if (v["model"] == G_POLL).any():
        R.p(
            "Die Polling-Serie (ohne simulierten Webhook) wird nur deskriptiv berichtet (kleine Stichprobe, "
            "andere Auslösebedingung)."
        )

    if "n_commands" in v.columns:
        R.p(
            "\nAusgeführte zustandsändernde Rollback-Befehle (n_commands, gezählt, keine Konstante):"
        )
        R.p(
            v.groupby("model")["n_commands"]
            .describe()[["count", "min", "50%", "max"]]
            .to_string()
        )
    if "order_pos" in v.columns:
        R.p(
            "\nDiagnose Reihenfolge: mittlere MTTR je Position im Lauf (1 = zuerst, 2 = danach):"
        )
        R.p(
            v.pivot_table(
                index="model",
                columns="order_pos",
                values="mttr_seconds",
                aggfunc=["mean", "count"],
            )
            .round(3)
            .to_string()
        )

    order = [m for m in (G_UF23, PUSH, G_POLL) if (v["model"] == m).any()]
    fig, ax = plt.subplots(figsize=(7, 5))
    rng = np.random.default_rng(SEED)
    for i, m in enumerate(order):
        vals = v[v["model"] == m]["mttr_seconds"].dropna().to_numpy()
        ax.boxplot(
            vals,
            positions=[i],
            widths=0.45,
            showfliers=False,
            patch_artist=True,
            boxprops=dict(facecolor=COLOR[m], alpha=0.45),
            medianprops=dict(color="black"),
        )
        ax.scatter(
            np.full(len(vals), i) + rng.uniform(-0.08, 0.08, len(vals)),
            vals,
            s=16,
            color=COLOR[m],
            edgecolor="black",
            linewidth=0.3,
            zorder=3,
        )
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([LABEL[m] for m in order])
    ax.set_ylabel("MTTR (s)")
    ax.set_title("UF2: Rollback-Zeit je Modell (gültige Läufe)")
    plt.tight_layout()
    plt.savefig(plots / "fig3_uf2_mttr.png", dpi=300)
    plt.close()


# ==============================================================================
# UF3
# ==============================================================================
def evaluate_uf3(path, plots):
    req = [
        "model",
        "run",
        "valid",
        "phase1_cluster_s",
        "phase2_operator_s",
        "phase3_workload_s",
        "total_s",
    ]
    df = load(path, req, "UF3")
    if df is None:
        return
    R.h("UF3: DISASTER RECOVERY / CLUSTER-RE-INITIALISIERUNG")
    R.p(
        "Phasen: 1 = Cluster-Provisioning (identischer Terraform-Befehl), 2 = Operator-Bootstrap (nur GitOps), "
        "3 = Workload-Rollout. Reihenfolge der Modelle wechselt je Lauf."
    )
    for c in ("phase1_cluster_s", "phase2_operator_s", "phase3_workload_s", "total_s"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    v = validity_report(df, "UF3")
    v["sum13_s"] = v["phase1_cluster_s"] + v["phase3_workload_s"]
    fam = "UF2/UF3 Hypothesentests (H2, H3b)"

    gv = v[v["model"] == G_UF23]
    pv = v[v["model"] == PUSH]
    metrics = [
        ("phase1_cluster_s", "Phase 1 Cluster"),
        ("phase3_workload_s", "Phase 3 Workloads"),
        ("sum13_s", "Phase 1 + 3 (ohne Operator)"),
        ("total_s", "Gesamtdauer"),
    ]
    for col, lab in metrics:
        R.p(f"\n{lab} (s):")
        R.p(describe_table({LABEL[G_UF23]: gv[col], LABEL[PUSH]: pv[col]}, digits=2))
        add_time_test(fam, f"UF3: {lab}", gv[col], pv[col], "Argo", "Push")
    R.p("\nPhase 2 Operator-Bootstrap (nur GitOps; beim Push-Modell nicht vorhanden):")
    R.p(describe_table({LABEL[G_UF23]: gv["phase2_operator_s"]}, digits=2))
    if len(gv) and len(pv):
        diff_total = gv["total_s"].mean() - pv["total_s"].mean()
        diff_13 = gv["sum13_s"].mean() - pv["sum13_s"].mean()
        R.p(
            f"\nMittlere Differenz Gesamtdauer (Argo - Push): {diff_total:.2f} s; davon ohne Operator-Phase "
            f"(Phase 1 + 3): {diff_13:.2f} s; Operator-Phase im Mittel: {gv['phase2_operator_s'].mean():.2f} s."
        )

    for col in ("n_commands", "n_credential_transfers"):
        if col in v.columns:
            R.p(
                f"\n{col} (gezählt; Zählregel: zustandsändernde Befehle bzw. Export von Admin-Zugangsdaten außerhalb des Clusters):"
            )
            R.p(
                v.groupby("model")[col]
                .describe()[["count", "min", "50%", "max"]]
                .to_string()
            )
    R.p(
        "Hinweis: Die Befehlszahlen werden beschreibend berichtet (keine Streuung, kein Test)."
    )

    if "order_pos" in v.columns:
        R.p("\nDiagnose Reihenfolge: mittlere Gesamtdauer je Position im Lauf:")
        R.p(
            v.pivot_table(
                index="model",
                columns="order_pos",
                values="total_s",
                aggfunc=["mean", "count"],
            )
            .round(2)
            .to_string()
        )
    for m, lab in ((G_UF23, LABEL[G_UF23]), (PUSH, LABEL[PUSH])):
        sub = v[v["model"] == m]
        rho, p, n = spearman_diag(sub["run"], sub["total_s"])
        R.p(
            f"Diagnose Zeittrend {lab}: Spearman(Lauf, Gesamtdauer) = {rho:.3f} (p = {fmt_p(p)}, n = {n})"
        )

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    labels = [LABEL[G_UF23], LABEL[PUSH]]
    p1 = [gv["phase1_cluster_s"].mean(), pv["phase1_cluster_s"].mean()]
    p2 = [
        gv["phase2_operator_s"].mean(),
        0.0,
    ]  # Push: kein Operator (bewusst 0 für die Darstellung)
    p3 = [gv["phase3_workload_s"].mean(), pv["phase3_workload_s"].mean()]
    axes[0].bar(labels, p1, color="#377eb8", width=0.5, label="1 Cluster-Provisioning")
    axes[0].bar(
        labels, p2, bottom=p1, color="#984ea3", width=0.5, label="2 Operator-Bootstrap"
    )
    axes[0].bar(
        labels,
        p3,
        bottom=np.array(p1) + np.array(p2),
        color="#4daf4a",
        width=0.5,
        label="3 Workload-Rollout",
    )
    axes[0].set_ylabel("Mittlere Dauer (s)")
    axes[0].set_title("UF3: Phasen (Mittelwerte, gültige Läufe; Push ohne Phase 2)")
    axes[0].legend(fontsize=8)

    rng = np.random.default_rng(SEED)
    for i, (col, lab) in enumerate((("sum13_s", "Phase 1 + 3"), ("total_s", "Gesamt"))):
        for off, m, sub in ((-0.18, G_UF23, gv), (0.18, PUSH, pv)):
            vals = sub[col].dropna().to_numpy()
            if len(vals):
                axes[1].boxplot(
                    vals,
                    positions=[i + off],
                    widths=0.3,
                    showfliers=False,
                    patch_artist=True,
                    boxprops=dict(facecolor=COLOR[m], alpha=0.45),
                    medianprops=dict(color="black"),
                )
                axes[1].scatter(
                    np.full(len(vals), i + off) + rng.uniform(-0.05, 0.05, len(vals)),
                    vals,
                    s=14,
                    color=COLOR[m],
                    edgecolor="black",
                    linewidth=0.3,
                    zorder=3,
                )
    axes[1].set_xticks([0, 1])
    axes[1].set_xticklabels(["Phase 1 + 3", "Gesamtdauer"])
    axes[1].set_ylabel("Dauer (s)")
    axes[1].set_title("UF3: Verteilung je Lauf")
    axes[1].legend(
        handles=[
            plt.Rectangle((0, 0), 1, 1, color=COLOR[G_UF23], alpha=0.6),
            plt.Rectangle((0, 0), 1, 1, color=COLOR[PUSH], alpha=0.6),
        ],
        labels=labels,
        fontsize=8,
    )
    plt.tight_layout()
    plt.savefig(plots / "fig4_uf3_disaster_recovery.png", dpi=300)
    plt.close()


# ==============================================================================
# MAIN
# ==============================================================================
def resolve_results_dir(arg):
    if arg:
        return Path(arg).resolve()
    for cand in (SCRIPT_DIR / "results", SCRIPT_DIR.parent / "results"):
        if (cand / "natural_selfheal_measurements.csv").exists():
            return cand
    return SCRIPT_DIR / "results"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", help="Verzeichnis mit den CSV-Dateien")
    ap.add_argument("--uf1", default="natural_selfheal_measurements.csv")
    ap.add_argument("--uf2", default="mttr_measurements_v2.csv")
    ap.add_argument("--uf3", default="disaster_recovery_measurements_v2.csv")
    args = ap.parse_args()

    results = resolve_results_dir(args.results_dir)
    plots = results / "plots"
    tables = results / "tables"
    plots.mkdir(parents=True, exist_ok=True)
    tables.mkdir(parents=True, exist_ok=True)

    R.h("AUSWERTUNGSBERICHT")
    R.p(f"Erstellt: {datetime.now(timezone.utc).isoformat(timespec='seconds')} UTC")
    R.p(
        f"Python {platform.python_version()}, pandas {pd.__version__}, numpy {np.__version__}, scipy {scipy.__version__}"
    )
    R.p(f"Ergebnisverzeichnis: {results}")
    R.p(
        f"Signifikanzniveau {ALPHA}; Mindestgröße je Gruppe für Tests: {MIN_N_TEST}; Seed {SEED}."
    )

    uf1 = evaluate_uf1(results / args.uf1, tables, plots)
    evaluate_h1a_model(uf1, plots)
    evaluate_uf2(results / args.uf2, plots)
    evaluate_uf3(results / args.uf3, plots)
    finalize_tests(tables)

    R.h("INTERPRETATIONSHINWEISE")
    for line in (
        "1. UF1 misst die Zeit bis zur Behebung (Erkennung + Heilung); die Push-Zeit enthält den Start von 'kubectl apply'.",
        "2. H1a wird modellbasiert dargestellt (Push-Wartezeit ~ U(0,T)), nicht als Test auf simulierte Daten.",
        "3. Zeiten gelten nur für erfolgreiche Läufe; Raten werden getrennt berichtet (kein Strafwert).",
        "4. Pro Muster liegen n Wiederholungen vor; gepoolte UF1-Werte sind gruppiert und nur deskriptiv.",
        "5. Nicht signifikant bedeutet nicht gleich; Aussagen über Gleichheit würden einen Äquivalenztest erfordern.",
        "6. Befehlszahlen (n_commands, n_credential_transfers) folgen einer festgelegten Zählregel und sind keine Messwerte mit Streuung.",
        "7. Alle Messungen stammen aus einer lokalen Kind-Umgebung; Reihenfolgeeffekte siehe Diagnoseabschnitte.",
    ):
        R.p(line)

    (results / "statistical_summary_v2.txt").write_text(
        "\n".join(R.lines) + "\n", encoding="utf-8"
    )
    print(
        f"\nBericht: {results / 'statistical_summary_v2.txt'}\nGrafiken: {plots}\nTabellen: {tables}"
    )


if __name__ == "__main__":
    main()
