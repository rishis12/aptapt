# aptapt v2 design

Decisions agreed on 2026-09-26. Madison only, and still a hackathon demo, so no real money moves.

## Buildings
Five real buildings are loaded statically from `app/data/buildings.json`: Seven20, The Bella, the Trinitas W. Mifflin project,
Johnson & Broome (Core Spaces) and Theory Madison. Photos are the official renderings, stored in
`app/static/img/buildings/<slug>/` with credits. They're fine for a private demo but must be replaced before any public launch.

Property managers are **fictional** and labeled as such. Each building either has a manager **on aptapt**
(an agent account) or **not on aptapt** (only a fictional contact email). The two follow different paths, described below.

## Accounts
Email and password sign-up and login, with passwords stored as werkzeug hashes. POST requests are protected by a CSRF token.
Leasing agents sign up through an agent invite link or a pitch link. Demo mode keeps a user switcher, and seeded accounts use the password `demo1234`.

## Money (all simulated)
Every payment goes through the same **test-mode checkout**. It accepts only test cards: `4242 4242 4242 4242` succeeds,
`4000 0027 6000 3184` triggers the bank-verification step, and `4000 0000 0000 0002` is declined. Any other number is rejected,
so real card data can't be entered. Only the brand and last 4 digits are stored. Everything is recorded in a `deposits` ledger.

| Deposit | Amount | When | Refunded | Forfeited |
|---|---|---|---|---|
| Commitment | $15 | Joining a committed group | Group misses its due date; you sign the lease; the deal is cancelled; you're released after rejecting a counter-offer | You leave the group at any time; you don't reserve before the window closes |
| Holding | $250 | Reserving after unlock | Deal cancelled | Kept by the property toward your first month's rent (simulated) |

## Committed group lifecycle
Each group has a **due date**: 30 days from when it starts, or a date the agent's offer sets.
If the due date passes without an unlock, the group **expires** and every commitment deposit is refunded.

**Manager on aptapt (offer path):**
`forming` → (3+ committed) invite agent → `agent_invited` → agent posts offer → `offer_active` → target met → `unlocked`

**Manager not on aptapt (pitch path):**
`forming` → the group sets its **ask** (incentive + size) → target met → `pitch_sent` (the pitch goes to the manager's contact through a simulated outbox, with a response link)
- Manager **accepts** → they become the building's agent on aptapt → `unlocked`
- Manager **counters** → members vote → people who reject are released with a refund → if the group is still at or above the counter's size → `unlocked`, otherwise `offer_active` with the counter as the offer
- Manager **declines**, or doesn't respond within 5 days → back to `forming` with the ask cleared; the group can set a new ask

When a group unlocks, the next committed group for that building starts forming automatically.

## After unlock (a "deal")
A 72-hour **reservation window** opens. Each member's checklist:
1. **Reserve**: floor plan and move-in date → application (fake values allowed) → $250 holding deposit through checkout → receipt
2. **Sign lease**: review the terms, then sign by typing your name. Signing refunds the $15 commitment deposit.

The agent's roster shows each member's progress, and the agent can nudge members.
When the window closes, anyone who hasn't reserved is dropped and forfeits their commitment deposit. If fewer than the target remain, the deal is **short**, and the agent chooses:
**honor** the deal anyway, **extend** by 48 hours and let new renters join, or **cancel** (every deposit refunded).
The deal is **closed** once every remaining member has signed.

## Deadlines
Deadlines are checked on each request, since there are only a few rows. Demo buttons can jump a group or deal to its deadline.
