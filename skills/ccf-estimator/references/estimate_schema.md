# CCF Estimate Output Schema

## Estimate JSON Structure

```json
{
  "project": {
    "name": "Sample Retail TI",
    "gc": "Example GC",
    "bid_date": "2026-03-18",
    "tier": "target",
    "labor_rate": 28.0,
    "overhead_pct": 0.12,
    "markup_pct": 0.20
  },
  "line_items": [
    {
      "area": "Interior Walls",
      "task": "Sales Floor Walls",
      "task_code": "walls_spray_new",
      "quantity": 7000,
      "unit": "SF",
      "coats": 2,
      "method": "Spray",
      "paint_system": "ProBlock Primer + ProMar 200 Eg-Shel",
      "prod_rate": 150,
      "labor_hours": 93.33,
      "labor_cost": 2613.33,
      "material_cost": 1680.0,
      "equipment_cost": 0,
      "subtotal": 4293.33,
      "notes": "Night work"
    }
  ],
  "category_subtotals": {
    "Interior Walls": {
      "labor_hours": 200.5,
      "labor_cost": 5614.0,
      "material_cost": 3200.0,
      "equipment_cost": 0,
      "subtotal": 8814.0
    }
  },
  "totals": {
    "labor_hours": 350.0,
    "labor_cost": 9800.0,
    "material_cost": 5000.0,
    "equipment_cost": 500.0,
    "direct_cost": 15300.0,
    "overhead": 1836.0,
    "subtotal_before_markup": 17136.0,
    "markup": 3427.2,
    "bid_price": 20563.2
  },
  "metrics": {
    "total_sf": 22000,
    "blended_rate_per_sf": 0.93,
    "crew_days_2_man": 22,
    "crew_days_3_man": 15
  }
}
```

## Takeoff Input Format

Each takeoff line item:

```json
{
  "area": "Interior Walls",
  "task": "Sales Floor Walls — spray P+2",
  "task_code": "walls_spray_new_drywall",
  "quantity": 7000,
  "unit": "SF",
  "method": "Spray",
  "coats": 2,
  "prod_rate": 150,
  "material_cost_per_sf": 0.12,
  "paint_system": "ProBlock Primer + ProMar 200 Eg-Shel",
  "notes": "Night work"
}
```

### Alternative fields for non-SF items:
- `hrs_per_unit`: hours per unit (doors, frames)
- `labor_hrs_override`: flat hours (LS items)
- `material_cost_flat`: flat material $ (LS items)
- `material_cost_per_unit`: $/EA (doors)
- `equipment_cost`: flat equipment/rental $
