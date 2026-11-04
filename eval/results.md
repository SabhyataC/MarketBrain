# Catalog evaluation

Video: `D:\Demo_Market_Brain\data\uploaded_video.mp4`  |  products on the shelf (hand-labelled): **14**  |  runs: **1**  |  business type: small shop

| Measure | Result |
|---|---|
| Products found (recall) | 100.0% |
| Draft items that are real products (precision) | 100.0% |
| Found products with name, brand and price all correct | 92.9% |
| Price correct | 100.0% |
| Wrong items the app flagged for the owner to check | 100.0% |
| Wrong items with NO flag (average per run) | 0 |
| Model calls per run | 10 (0.71 per product) |
| Estimated cost per product (in the units set in .env) | unknown: set COST_VISION in .env |
| Time for the app to build the draft | 85.4 s |
| Time to list them by hand | 1680.0 s |
| Time with the app, including owner review | unknown: pass --review-seconds-per-product |
| Time saved before owner review | 1594.6 s (94.9%) |

## What went wrong (first run)

- **LINEN SHIRT.**: wrong name. Read as {'name': 'LINEN SHRT', 'brand': 'EARTH WEAR', 'price': 1099.0}, expected {'name': 'LINEN SHIRT.', 'brand': 'EARTH WEAR', 'price': 1099.0}. Flagged: seen in only one frame
