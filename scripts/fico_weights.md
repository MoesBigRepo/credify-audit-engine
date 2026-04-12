# FICO Score 8 — Weights & Impact Reference

## Component Weights

| Component | Weight | Key Factors |
|-----------|--------|-------------|
| Payment History | 35% | On-time payments, late payments (30/60/90/120), collections, charge-offs, bankruptcies |
| Amounts Owed | 30% | Credit utilization ratio, total balances, per-card utilization |
| Length of Credit History | 15% | Age of oldest account, AAOA (all accounts), age of newest account |
| Credit Mix | 10% | Revolving (cards), installment (loans), mortgage — diversity matters |
| New Credit | 10% | Hard inquiries (last 12mo), new accounts opened recently |

## Authorized User (AU) Treatment in FICO 8

**Critical: FICO 8 substantially reduces piggybacking benefit.**

| Factor | AU Counted? | Notes |
|--------|------------|-------|
| AAOA / Length of History | **NO** | AU accounts excluded from age calculations |
| Credit Utilization | **NO** | AU balance and limit excluded from utilization |
| Payment History | **YES** | AU account's payment record counts (positive or negative) |
| Credit Mix | **Partial** | Adds to account count / type diversity, but reduced weight |
| Account Count | **YES** | Counts toward "too few accounts" factor |

### Practical AU Impact Ranges

| Scenario | Typical Impact | Notes |
|----------|---------------|-------|
| Thin file (<5 accts), adding 1 AU | +20 to +50 pts | Mainly from payment history + account count |
| Thin file, adding 2 AUs | +25 to +60 pts | Diminishing returns on 2nd AU |
| Thick file (10+ accts), adding 1 AU | +0 to +15 pts | Marginal benefit when file already has depth |
| Clean profile (0 derog), adding AU | +10 to +30 pts | Benefit mainly from mix/count, not derog offset |

**Key insight**: AU tradelines do NOT boost AAOA or utilization under FICO 8.
The primary benefit is: (1) additional positive payment history lines, (2) moving
out of "thin file" scorecard, (3) marginal credit mix improvement.

## Derogatory Removal Impact

### First Derog Principle
The first negative item hurts the most. A single collection on a clean 780 profile
can drop 100-150 points. Removing it recovers most of that. Additional derogs have
progressively less marginal impact.

### Collection Removal

| Profile Context | Points Recovered | Notes |
|----------------|-----------------|-------|
| Clean profile + 1 recent collection (<1yr) | +50 to +100 pts | Largest recovery |
| Clean profile + 1 old collection (3-5yr) | +20 to +40 pts | Age diminishes impact |
| Multiple derogs, removing 1 of many | +5 to +25 pts | Diminishing returns |
| Removing last remaining derog | +30 to +60 pts | Scorecard shift bonus |

### Charge-Off Removal

| Profile Context | Points Recovered | Notes |
|----------------|-----------------|-------|
| Single charge-off, otherwise clean | +50 to +100 pts | Similar to collection |
| Charge-off with balance vs $0 | +10 to +20 extra | Balance removal adds utilization benefit |
| Old charge-off (5-7yr) | +15 to +30 pts | Significantly diminished by age |

### Late Payment Removal

| Severity | Points Recovered | Notes |
|----------|-----------------|-------|
| Single 30-day late (recent) | +10 to +30 pts | Least severe |
| Single 60-day late | +20 to +40 pts | Moderate |
| Single 90-day late | +30 to +50 pts | Severe |
| Single 120+ day late | +40 to +60 pts | Near charge-off severity |
| Old late payment (3+ years) | +5 to +15 pts | Time diminishes impact |

### Diminishing Returns Formula
When removing multiple derogs, approximate the pattern:
- 1st removal: 100% of typical impact
- 2nd removal: ~70% of typical impact
- 3rd removal: ~50%
- 4th removal: ~35%
- 5th+ removal: ~25%

The last removal can spike higher due to scorecard shift (moving from
"derogatory" scorecard to "clean" scorecard).

### Derog Age Decay (Deep Research Finding)
The age of a derogatory item determines how many points are recovered on removal:

| Derog Age | Recovery Multiplier | Notes |
|-----------|-------------------|-------|
| <1 year | 1.0 (full impact) | Recent items carry maximum penalty |
| 1-2 years | 0.80 | Still heavily weighted |
| 2-3 years | 0.60 | Moderate decay |
| 3-5 years | 0.40 | Significant decay |
| 5-6 years | 0.20 | Near end of relevance |
| 6-7 years | 0.10 | Almost no recovery — already aged out |

A 6-year-old collection removal may only recover 0-15 points because the model
has already naturally phased out most of its penalty over time.

### Derog Floor Effect
At **8+ negative items**, the marginal impact of each additional derog becomes zero.
The profile is fully suppressed. Removing individual items from a heavily burdened
file yields negligible gains until a significant portion (usually all) are cleared.

## Utilization Thresholds

FICO uses precise decimal breakpoints, not rounded integers.

| Utilization | Impact | Breakpoint |
|------------|--------|------------|
| 0% | Slightly negative (no activity signal) | — |
| 1-3% | Optimal — maximum points | — |
| 4-8.9% | Excellent — minimal penalty | **8.9%** is a key threshold |
| 9-19% | Good — small penalty starts | Crossing 9% triggers first real penalty |
| 20-28.9% | Fair — moderate penalty | — |
| 29-49% | Poor — significant penalty | **28.9%** is a major threshold |
| 50-74% | Bad — heavy penalty | — |
| 75%+ | Very bad — severe penalty | — |

**Key finding**: The breakpoints are at **8.9%** and **28.9%**, not rounded 9%/29%.
Crossing these decimal-specific boundaries triggers the most significant score shifts.

## Credit Age Thresholds

FICO weights oldest account age and AAOA differently. Oldest account triggers
"mature" classification at 36 months. AAOA benefit plateaus at ~90 months (7.5yr).

| AAOA | Impact | Notes |
|------|--------|-------|
| <1 year | Very thin — scorecard penalty | — |
| 1-2 years | Thin file zone | — |
| 2-3 years | Emerging — modest benefit | Oldest acct reaching 36mo = "mature" classification |
| 3-5 years | Established — solid benefit | — |
| 5-7 years | Strong — near-maximum benefit | — |
| 7.5 years (90mo) | **Plateau** — maximum benefit reached | No additional gain beyond this |
| 7.5+ years | No incremental improvement | AAOA benefit is fully maxed |

**Key finding**: There is NO additional scoring benefit for AAOA beyond 90 months.
A 15-year AAOA scores identically to a 7.5-year AAOA for the age component.

## Score Ceiling Effects

- Above 750: each additional positive factor adds fewer points
- Above 780: very difficult to gain more than 5-10 pts from any single action
- The 800-850 range requires: 0 derog, low util, 10+ year history, diverse mix, no recent inquiries
- Adding tradelines to an already-excellent profile has minimal effect

## Scorecard Assignment

FICO uses **12 internal scorecards** — 8 for clean profiles, 4 for those with
negative marks. Consumers are assigned to a scorecard before scoring begins.

**Scorecard categories:**
- Clean/thick file (most favorable — established history, no derogs)
- Clean/thin file (limited history, no derogs)
- Clean/young file (short oldest account, no derogs)
- Derogatory file (any negative marks present)
- Severely derogatory (bankruptcy, multiple charge-offs)

**Critical: Scorecard shift can cause DROPS**
Moving from a "dirty" to "clean" scorecard subjects the profile to stricter
utilization standards. If the client has high balances, the clean scorecard
may penalize utilization more harshly than the derogatory scorecard did.

| Transition | Typical Impact | Risk |
|-----------|---------------|------|
| Dirty → Clean (removing last derog) | +40 to +80 pts | May drop if util >30% |
| Thin → Thick (crossing 5 accounts) | +15 to +25 pts | Minimal risk |
| Young → Mature (oldest acct hits 36mo) | +10 to +20 pts | Minimal risk |

## Thin File Threshold

The transition from thin to thick file occurs at **exactly 5 tradelines**.
This triggers a scorecard shift worth +15 to +25 points.
- 3 accounts = minimum for scoring (some models)
- 4 accounts = still "thin"
- **5 accounts = "thick" — scorecard shifts**
- Secured credit builder products (Self, Chime, Austin Capital Bank) count
  as tradelines for this threshold — they are not discounted.

---

Sources: myFICO, Experian, Credit Karma, Doctor of Credit, Superior Tradelines,
myFICO Forums, CFPB, FICO Blog, Gemini Deep Research (April 2026)
