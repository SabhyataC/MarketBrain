# Meta Hackathon Checklist: Marketing Brain

Build window: Oct 26 to Nov 4, 2026. Real cutoff is 07:30 AM IST on Nov 5, but aim to submit by the afternoon of Nov 4.

## Prep (Oct 23 to Oct 25)

**Today, Oct 23**
- [ ] Create the Meta developer app
- [ ] Create a Facebook Page
- [ ] Create an Instagram professional account
- [ ] Create a test ad account
- [ ] Read the Marketing API access requirements
- [ ] Create the Meta Model API key
- [ ] Make one working test request with the OpenAI SDK

**Oct 24**
- [ ] Set up a Shopify dev store with 20+ products
- [ ] Film the shelf video (10-20 real products)
- [ ] Hand-label the product list (this is your accuracy test set)
- [ ] Note the manual baseline: time to list one product

**Oct 25**
- [ ] Create the repo and FastAPI skeleton
- [ ] Create the model client
- [ ] Fix team roles
- [ ] Name one person who merges to main daily
- [ ] Read the Official Rules and accept the waiver
- [ ] Check what the rules say about teams and tracks

## Build (Oct 26 to Nov 4)

**Day 1, Oct 26 (light day)**
- [ ] Read the rules again if they changed
- [ ] Everyone can run the repo
- [ ] API works from every teammate's machine

**Day 2, Oct 27: Foundation**
- [ ] Database set up
- [ ] Shopify read/write adapter
- [ ] Approval queue
- [ ] Audit log
- [ ] Budget cap enforced in backend code
- [ ] CHECK: read a product, approve a proposal, see it logged

**Day 3, Oct 28: Camera-to-catalog**
- [ ] Frame sampling from the video (ffmpeg)
- [ ] SAM 3.1 segments products
- [ ] Muse Spark identifies them
- [ ] CHECK: shelf video becomes a catalog draft

**Day 4, Oct 29: Planner**
- [ ] Correction UI for the catalog draft
- [ ] Planner picks 3 products with reasons (stock and margin)
- [ ] DECIDE THE CUT LINE
- [ ] CHECK: planner returns 3 proposals with reasoning

**Day 5, Oct 30: Ad engine**
- [ ] Ad copy variants
- [ ] Ad images (Muse Image)
- [ ] Policy pre-check for risky claims
- [ ] CHECK: ad copy and image appear in the approval queue

**Day 6, Oct 31: Meta draft**
- [ ] Paused ad draft in the test ad account
- [ ] If Meta API is blocked, show the draft in your own queue and say so
- [ ] Optional: SEO diff with apply and rollback

**Day 7, Nov 1: Report**
- [ ] Weekly report from real data
- [ ] Cost and latency logging for every model call
- [ ] Stretch only: forecast vs naive baseline

**Day 8, Nov 2: Evaluate and freeze**
- [ ] Run the evaluation harness
- [ ] Catalog accuracy number
- [ ] Cost per product number
- [ ] Time-saved number
- [ ] Fix bugs
- [ ] FEATURE FREEZE at end of day

**Day 9, Nov 3: Rehearse**
- [ ] Run the full demo twice without edits
- [ ] Record the demo video
- [ ] Record a backup video
- [ ] Show one honest failure and its correction
- [ ] Write the README with setup steps

**Day 10, Nov 4: Submit**
- [ ] Submission text
- [ ] Architecture diagram
- [ ] Model-routing table (which model did what, at what cost)
- [ ] Notes on guardrails and known limits
- [ ] UPLOAD BY THE AFTERNOON, not at the deadline

## Only after the demo path works twice
- [ ] Forecasting and budget bandit
- [ ] Voice commands
- [ ] Brand-consistent ad series
- [ ] Open-model bulk run

## Rules to remember
1. Submit by Nov 4 evening IST at the latest.
2. If the Meta ad API is blocked, show the ad as a draft in your own approval queue and say so plainly.
3. Anything not on this list stays out until Day 8 is done and the demo path works.
4. Nov 5 to 12 is for the PayPal hackathon (Paymaster). Do not let Meta overflow into it.

We would have to cut the scope to the demo path and make that part excellent:
- Camera-to-catalog: shelf video to catalog draft, with the owner correcting it. This is your most unique feature.
- Planner: picks 3 products to promote, with reasons, using stock and margin.
- Ad draft: copy and image, as a paused draft behind the approval queue.
- Approval queue, audit log, and budget cap.
- Evaluation numbers: catalog accuracy, cost per product, and time saved.

# Things to do if we finish early:
- Glimmer fine-tune, 
- landing pages, and 
- SEO pull requests
