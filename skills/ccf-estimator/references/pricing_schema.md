# CCF Pricing Config Schema

The pricing config JSON (`data/pricing/ccf-pricing-config.json`) has this structure:

## production_rates

### painting[] — SF/hr rates per painter
- `task`: Description (e.g. "New drywall — prime + 2 coats")
- `category`: Group (e.g. "WALLS — Brush & Roll", "CEILINGS")
- `unit`: Rate unit (SF/hr, LF/hr, hr/door, hr/frame)
- `slow_rate`: Conservative rate (use for Floor tier)
- `avg_rate`: Average rate (use for Target tier)
- `fast_rate`: Aggressive rate (use for Premium tier)
- `coats`: Coat system (1P+2F, 2, per coat, etc.)
- `notes`: Application notes

### prep[] — Prep work rates
Same structure as painting. Includes masking, sanding, caulking, cleaning, wallpaper removal, spot priming.

### non_painting[] — Non-painting trades we bid
- `task`: Trade description
- `unit`: Pricing unit ($/SF, $/stall, $/unit, $/LF)
- `low_rate`: Low end of market
- `market_rate`: Market range
- `notes`: Details

## pricing_policy

### labor_rates[] — NC market labor rates
- `role`: Worker type
- `hourly_wage`: Base pay range
- `burden_pct`: Burden percentage (WC/FICA/GL)
- `burdened_rate`: All-in hourly rate
- `market_comparison`: NC market data

### unit_prices[] — Competitive $/SF pricing
- `task`: Scope item
- `unit`: Pricing unit ($/SF, $/LF, $/door)
- `floor_price`: [low, high] — break-even
- `target_price`: [low, high] — competitive
- `premium_price`: [low, high] — relationship
- `when_to_use`: Guidance

### markup_scenarios[] — When to apply what markup
- `scenario`: Situation name
- `markup_range`: [low, high] as decimals
- `margin_range`: [low, high] as decimals
- `when_to_apply`: Guidance text

### overhead[] — Overhead cost items
- `item`, `monthly`, `annual`, `pct_of_revenue`

### bid_formula[] — 5-step bid pricing formula
Steps as text strings
