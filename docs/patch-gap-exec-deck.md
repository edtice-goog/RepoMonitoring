# The Patch Gap — Executive Briefing

> **Audience:** managers / executives unfamiliar with patch-gap monitoring
> **Goal:** establish, with citations, that N-day / patch-gap exploitation is a *real, documented, and accelerating* threat — and why embedded/OSS vendors are hit hardest.
> **Format note:** content-first deck. Slides separated by `---` (Marp/reveal-style) so this converts cleanly to PowerPoint or a technical variant later. Each slide has **Speaker notes** and a **Source** line. Every number carries its org + year.
> **Title convention:** the `## Slide N — …` heading is an *organizing note for this doc only* — it does not ship. The line marked **TITLE:** is the neutral text that goes on the actual slide.

---

## Slide 1 — Title

**TITLE:** The Patch Gap

# The Patch Gap
### Defending the window before a CVE exists

*Why a public security fix is the starting gun for attackers — and why our firmware is still in the blocks.*

**Speaker notes:** Today's claim, up front: the most dangerous moment in a vulnerability's life isn't when it's announced — it's the quiet window between the fix becoming *visible* and our product being *rebuilt*. That window is measurable, it's shrinking, and embedded vendors sit at the wrong end of it. Everything that follows is sourced.

---

## Slide 2 — The problem in one sentence

**TITLE:** The Exposure Window

> **A security fix becomes public knowledge the moment it's committed — not when the CVE is announced. Attackers race us through that gap.**

- The fix itself *is* the disclosure. The code change tells you exactly what was broken.
- We don't ship the moment upstream fixes a bug — we have to rebuild and re-release firmware.
- The difference between those two timestamps is our **exposure window**.

**Speaker notes:** This is the whole thesis. If the room remembers one slide, it's this one. The rest of the deck is evidence that (a) attackers exploit this window, (b) the window is collapsing, and (c) it's widest for embedded.

**Source:** Google Project Zero, *"vulnerabilities become public knowledge as soon as a software update is released, not when they are announced in release notes"* (2020).

---

## Slide 3 — How the attack actually works (no code required)

**TITLE:** How the attack works

**"Patch diffing" — reverse-engineering the fix to find the flaw:**

1. Vendor publishes an update (or an open-source project merges a commit).
2. Attacker compares "before" vs. "after" — the change points straight at the vulnerability.
3. Attacker builds a working exploit from that diff.
4. Anyone still running the old version is now a target.

This is routine enough to have a nickname: **"Patch Tuesday, Exploit Wednesday."**

> **Self-evident, once stated:** a defect that was found, reported, and fixed is a defect with a *map*. The commit **is** the map — it points an adversary straight back at the flaw. Re-finding something already located for you is a low bar.

**Speaker notes:** Make it concrete but non-technical. IBM X-Force researchers took a public Windows patch, diffed it with off-the-shelf tools, and had a working exploit *in about a day* — explicitly noting they had no prior experience with that kernel component. The technique is not exotic; it's a standard skill. Project Zero set the *upper* bound at roughly 3 weeks to go from patch to a working crash.

**Source:** IBM X-Force, "Patch Tuesday, Exploit Wednesday" — CVE-2023-21768 weaponized "in about a day" (2023); Google Project Zero, ~3-week upper bound to diff a patch and build a PoC (2020).

---

## Slide 4 — The clock is the enemy: the window is collapsing

**TITLE:** Time-to-exploit is collapsing

**Mandiant average time-to-exploit (TTE), after a fix is available:**

| Period | Avg. days to exploit |
|---|---|
| 2018–2019 | **63 days** |
| 2020–2021 | 44 days |
| 2021–2022 | 32 days |
| **2023** | **5 days** |

> *"This is less than a sixth of the previously observed TTE."* — Mandiant

**This is a floor, not a ceiling:** the data ends in **2023**. Building exploits keeps getting cheaper and faster — an accelerant on an already-downward line only bends it further. The point stands on its own; no extra study required.

**Speaker notes:** This is the headline chart — render it as a steep downward bar/line. Five years ago, defenders had two months. In 2023 the average was five days. One caveat to keep in your pocket: the 5-day average excludes 15 statistical outliers (with them, ~47 days) and blends zero-day and N-day timing; the *direction* is corroborated by independent press (Help Net Security, SC Media, TechRepublic). On the trend continuing: treat 5 days as the optimistic case — the cost of exploit development has only fallen since. Worth noting the double edge: the same automation that speeds an attacker's patch-diffing also powers *our* commit triage, so this cuts in our favor too.

**Source:** Mandiant / Google Cloud, *"How Low Can You Go? An Analysis of 2023 Time-to-Exploit Trends"* (Oct 2024).

---

## Slide 5 — "N-day": attacks that happen *after* the fix is public

**TITLE:** N-day: exploitation after a patch exists

Of the **138** vulnerabilities Mandiant saw exploited in 2023, **41 (30%) were N-days** — first exploited *after* a patch existed. Of those N-days:

- **12%** exploited within **1 day** of the patch
- **29%** within **1 week**
- **56%** within **1 month**

**This is the patch-gap window, measured directly.**

**Speaker notes:** Slides 4 and 5 are a pair. N-day is the precise thing we're defending against — it's the share of attacks that target people who *had a fix available but hadn't shipped it*. More than half of these hit within a month. For an embedded vendor whose rebuild-and-release cycle is measured in months, "within a month" means the attack lands before we've even shipped. Honesty note: the N-day sample is n=41, so cite the raw counts (5 / 12 / 23 of 41) if pressed on statistics.

**Source:** Mandiant / Google Cloud, 2023 Time-to-Exploit report (Oct 2024).

---

## Slide 6 — You can't watch what hasn't been announced: silent fixes

**TITLE:** Silent fixes: patches precede disclosure

Open-source fixes are routinely public in the code **before any CVE or advisory exists**:

- **~70%** of security patches are committed **before** public disclosure.
- Vulnerability databases (Snyk, NVD) lag a **median of 25 days** behind the fix.
- **~38%** of security releases ship with **no note** that they fix a security issue ("silent fixes").
- Linux kernel study: fixes detectable in the public repo **2–179 days** before disclosure.

**Speaker notes:** This is why "just subscribe to a CVE feed" is not enough — by the time the CVE exists, the attacker has had the diff for weeks. The defender who only watches advisories is structurally late. These are peer-reviewed findings (ACM CCS, IEEE TSE, ACM CCSW), not vendor marketing. The Linux number matters most for us: it's the exact ecosystem under embedded firmware.

**Source:** Li & Paxson, ACM CCS 2017 (~70% before disclosure); Imtiaz et al., IEEE TSE 2022 (25-day median, 61.5% documented → ~38% silent); Ramsauer et al., ACM CCSW 2020 ("The Sound of Silence," 2–179 day Linux head start).

---

## Slide 7 — Why embedded is hit hardest (this is us)

**TITLE:** Embedded carries the widest exposure

**Forescout teardown of real router/edge firmware (2024):**

- Average open-source component is **5 years, 6 months old** — and **4 years, 4 months behind** the latest release.
- **161 known vulnerabilities** baked into the average firmware image (24 rated *critical*).
- **~20 exploitable N-day kernel vulnerabilities** per image.
- **4 of 5** images analyzed ran **OpenWrt** (embedded Linux) — the ecosystem our products live in.

**Speaker notes:** This is the slide that makes it personal. Embedded products carry huge, old dependency trees. Every one of those 161 known vulnerabilities had a public fix at some point that didn't make it into the shipped image. The patch gap isn't theoretical for embedded — it's the steady-state condition. Our long rebuild cycles + large dependency trees = the widest exposure window of anyone.

**Source:** Forescout, *"Rough Around the Edges"* OT/IoT router firmware study (Aug 2024).

---

## Slide 8 — Attackers already know embedded is the soft target

**TITLE:** Attackers concentrate on edge devices

- **Over 60%** of enterprise zero-day exploitation in 2024 hit **security & network devices** (20 of 33) — Ivanti, Palo Alto, Cisco ASA, Fortinet, VMware.
- **~half** of enterprise zero-days in 2025, same pattern.
- Why: **edge/embedded devices can't run endpoint detection (EDR)** — Mandiant calls this *"a blind spot for defenders… an ideal attack surface."*

**Speaker notes:** Two reinforcing facts: attackers are *concentrating* on the device class we build, and that device class is exactly where defenders are blindest because you can't install monitoring agents on a router or a camera. Just-over-half of nation-state zero-day activity now focuses on these technologies. The defensive answer to a blind spot you can't instrument *on-device* is to watch the supply chain *upstream* — which is what our tool does.

**Source:** Mandiant / Google Cloud, 2024 Zero-Day Trends & 2025 Zero-Day Review; corroborated by CISA/NSA Five Eyes edge-device guidance.

---

## Slide 9 — The scale and the business impact

**TITLE:** Scale and business impact

- **+180% (≈3×)** year-over-year surge in breaches that start with vulnerability exploitation — *Verizon DBIR 2024.*
- Organizations take **55 days** to remediate just **half** of critical vulns after a patch ships — *Verizon DBIR 2024.* (Attackers need 5.)
- **CISA Known-Exploited-Vulnerabilities catalog: 1,484 entries**, +245 (**+20%**) in 2025 — its largest jump in three years.
- Average breach cost **$4.88M**, +10% YoY (largest jump since the pandemic) — *IBM, 2024.*
- Named example: **PAN-OS CVE-2024-3400** — public PoC one day after disclosure; **a dozen-plus groups** exploiting it (incl. a ransomware affiliate) **within two weeks.**

**Speaker notes:** This is the "so what, in dollars and trend lines" slide. The 55-days-vs-5-days contrast is the rhetorical knockout: defenders patch in 55 days, attackers exploit in 5. PAN-OS is the named, recent, embedded-adjacent case that makes it real. Note: the DBIR/KEV/IBM figures are from primary sources but were *not* run through our adversarial verification pass — flagged in the source appendix.

**Source:** Verizon DBIR 2024; SecurityWeek on CISA KEV (2025); IBM Cost of a Data Breach 2024; Mandiant M-Trends 2025 (PAN-OS).

---

## Slide 10 — This is an established field, not a fringe idea

**TITLE:** A recognized field: tools, standards, research

*(The direct answer to "where's the evidence?")*

- **CISA** maintains a government catalog of actively-exploited vulns and mandates remediation deadlines (KEV / BOD).
- **Google OSV**, **OpenSSF cve-bin-tool**, **Timesys Vigiles** — production tools built specifically to find vulnerable components in builds (incl. **Buildroot** & **Yocto**).
- **"Security Patch Detection"** is a named academic discipline — a 2024 *ACM Computing Surveys* paper reviews **127 studies (2014–2023)** on automatically identifying which commits are security fixes.

**Speaker notes:** This slide exists because of the pushback you got. The threat model isn't our invention — governments legislate around it, Google and the Linux Foundation ship tools against it, and there's a decade of peer-reviewed literature treating "which commit is a silent security fix?" as a formal research problem. What's novel in our work isn't the threat; it's applying it as continuous *monitoring* for our specific vendor supply chain.

**Source:** CISA KEV; google/osv.dev; ossf/cve-bin-tool; TimesysGit/vigiles-buildroot; Lin et al., *ACM Computing Surveys* (2024), DOI 10.1145/3694782.

---

## Slide 11 — Where our work fits

**TITLE:** Where our work fits

**RepoMonitoring closes the gap on the one axis we control: defender reaction time.**

1. **Watch** the upstream repos our products depend on — including commits, before any CVE.
2. **Triage** each change: is this a *security fix* or a normal bug fix? (the hard, recognized problem from Slide 10)
3. **Alert** our build teams to rebuild *during* the gap — not 25 days later when the CVE finally posts.

> We can't make attackers slower. We can stop being the slowest one in the room.

**Speaker notes:** Tie every prior slide to this. Slide 6 says the signal exists in the code before the CVE. Slide 10 says detecting it is a solved-enough problem to build on. This slide says: we operationalize that for *our* dependency tree. Defensive, triage-only — we flag what to rebuild; we never derive exploits.

**Source:** Internal — RepoMonitoring project scope.

---

## Slide 12 — The ask

**TITLE:** The ask

- **Recognize** patch-gap exposure as a tracked risk for our embedded products (not just "patch when the CVE lands").
- **Resource** continuous upstream monitoring + commit triage for our core dependency set.
- **Target metric:** shrink our *fix-visible → firmware-shipped* window — the one number this entire deck is about.

**Speaker notes:** Keep the ask concrete and small. We're not asking to boil the ocean; we're asking to measure and shrink one specific window for a defined dependency set. Close by returning to Slide 4: the industry went from 63 days to 5. The question for us is simply: how many days are *we*, and are we moving in the right direction?

---

## Slide 13 — Appendix A: Full source list (org + year)

**TITLE:** Appendix A — Sources

**Primary threat-intelligence (institutional):**
- Mandiant / Google Cloud — *2023 Time-to-Exploit Trends* (Oct 2024) — 63→5 day TTE; N-day 12%/29%/56%.
- Mandiant / Google Cloud — *2024 Zero-Day Trends* & *2025 Zero-Day Review* — edge-device targeting, EDR blind spot.
- Mandiant — *M-Trends 2025* — exploitation = #1 initial vector 5 yrs running; PAN-OS CVE-2024-3400.
- Google Project Zero (M. Stone) — *"TFW you-get-really-excited-you-patch-diffed…"* (2020) — fixes are public at commit time.
- IBM X-Force — *Patch Tuesday, Exploit Wednesday* (2023) — CVE-2023-21768 weaponized in ~1 day.

**Embedded / scale / cost:**
- Forescout — *Rough Around the Edges* router firmware study (2024) — 5.5-yr-old components, 161 vulns/image, OpenWrt.
- Verizon — *DBIR 2024* — +180% vuln-exploitation surge; 55-day median to patch half of critical vulns.
- SecurityWeek — *CISA KEV expanded 20% in 2025* — 1,484 entries.
- IBM — *Cost of a Data Breach 2024* — $4.88M avg, +10% YoY.

**Peer-reviewed academic (silent fixes / detection):**
- Li & Paxson — *A Large-Scale Empirical Study of Security Patches*, ACM CCS 2017 — ~70% patched before disclosure.
- Imtiaz et al. — *Open or Sneaky?*, IEEE TSE 2022 — 25-day median DB lag; ~38% silent.
- Ramsauer et al. — *The Sound of Silence*, ACM CCSW 2020 — 2–179 day Linux head start.
- Wang/Sun et al. — *secret patches*, DSN 2019; Dong/Susilo/Sun et al., IEEE 2025 — silent patching prevalence.
- Lin et al. — *Vulnerabilities & Security Patches Detection in OSS: A Survey*, ACM Computing Surveys 2024.

**Tooling (recognized problem domain):**
- google/osv.dev · ossf/cve-bin-tool · TimesysGit/vigiles-buildroot · CISA KEV catalog.

---

## Slide 14 — Appendix B: Answering the skeptic (keep in your back pocket)

**TITLE:** Appendix B — Anticipated objections

**"The 5-day number sounds cherry-picked."**
→ It excludes 15 outliers; with them it's ~47 days, *still down from 63*. And it's corroborated by independent press and the academic literature. The trend is the point.

**"Those are vendor marketing reports."**
→ Mandiant/Google and IBM publish their methodology and sample sizes; the core mechanism (silent fixes, patch diffing) is independently established in peer-reviewed venues (CCS, TSE, CCSW).

**"The silent-fix percentages are general OSS, not our embedded stack."**
→ Correct, and we say so. The *embedded* exposure is proven separately by Forescout's firmware teardown and Mandiant's edge-device targeting data. The two halves meet at the Linux kernel — proven on both sides.

**"Show me one concrete case."**
→ PAN-OS CVE-2024-3400: public PoC one day after disclosure, a dozen-plus groups exploiting within two weeks (Mandiant, 2025).

**Do-not-use (failed our verification):**
- ❌ "Linux fixes precede CVE assignment by ~100 days (Kroah-Hartman)" — this specific claim was **refuted** in our fact-check; do not cite it.

**Speaker notes:** This slide is your insurance against a repeat of the last presentation. Every likely objection has a one-line, sourced rebuttal. The "do-not-use" entry matters: it keeps a tempting-but-unverified stat out of your mouth.

---

### Open items to strengthen later (noted for honesty, not for the room)
- A named *consumer* embedded mass-exploitation campaign with figures (RondoDox/Edimax/AVTECH camera botnets were found but not fully verified here).
- DBIR / KEV / IBM cost figures are from primary sources but were **not** run through the adversarial verification pass — quick re-check recommended before a high-stakes audience.
