# FeneVision Import Guide

This guide explains how to convert Lindsay Windows FeneVision exports to the app's CSV format using the `import_fenevision` mapper.

## Quick Start

```python
from src.import_fenevision import import_fenevision, export_app_csv

# Import FeneVision CSV
orders, errors = import_fenevision('path/to/fenevision_export.csv')

# Check for errors
if errors:
    for e in errors:
        print(f"Row {e['row']} (order {e['order_number']}): {e['reason']}")

# Export to app CSV format
export_app_csv(orders, 'output.csv')
```

## FeneVision CSV Format (Required Fields)

The FeneVision export must include these columns:

| Field | Type | Notes |
|-------|------|-------|
| `order_number` | string | Order ID (becomes `order_id` in app) |
| `window_width` | float | Width in inches |
| `window_height` | float | Height in inches |
| `ship_qty` | float | Quantity of windows |
| `ship_to_name` | string | Customer name |
| `ship_to_street` | string | Street address |
| `ship_to_city` | string | City |
| `ship_to_state` | string | State code |
| `ship_to_zip` | string | ZIP code |

### Optional Fields (captured in notes)

- `route_description` — added to order notes
- `target_ship_date` — added to order notes

## Field Mapping

| FeneVision | App CSV | Logic |
|------------|---------|-------|
| `order_number` | `order_id` | Direct copy |
| `ship_to_name` | `customer_name` | Direct copy |
| `ship_to_street`, `_city`, `_state`, `_zip` | `address` | Concatenated with spaces |
| `window_width × window_height × ship_qty` | `capacity_units` | Calculated (sq ft for standing windows) |
| — | `priority` | Always 0 (normal); no priority in FeneVision |
| `route_description`, `target_ship_date` | `notes` | Combined if present |

## Capacity Calculation

Windows stand upright in the truck, so:

```
capacity_units = window_width × window_height × ship_qty
```

All dimensions from FeneVision are assumed to be in inches; the result is in square feet (divided by 144).

## Validation & Error Handling

The mapper validates:
- ✓ All required fields are present
- ✓ Numeric fields (width, height, qty) are valid
- ✓ Dimensions and qty are positive (qty ≥ 0)
- ✓ Address can be empty/partial

### Handling Errors

Invalid rows are skipped and returned in the `errors` list. Each error includes:

```python
{
    "row": <line number in CSV>,
    "order_number": <order ID>,
    "reason": <error message>
}
```

Valid rows are still imported; you can review errors and re-export if needed.

## Example Workflow

1. **Export from FeneVision** — get CSV with all window orders
2. **Run mapper:**
   ```bash
   python3 << 'EOF'
   from src.import_fenevision import import_fenevision, export_app_csv
   orders, errors = import_fenevision('fenevision_export.csv')
   print(f"Valid: {len(orders)}, Errors: {len(errors)}")
   for e in errors:
       print(f"  Row {e['row']}: {e['reason']}")
   export_app_csv(orders, 'app_orders.csv')
   EOF
   ```
3. **Upload to app** — use "Upload CSV" to load `app_orders.csv`
4. **Generate load plan** — optimizer routes the orders

## Testing

Sample files in `sample_data/`:
- `example_fenevision.csv` — realistic FeneVision export (6 orders)
- `fenevision_with_issues.csv` — test error handling

## Python Version

Requires **Python 3.6+** (uses f-strings and pandas). Use `python3` in commands.

## Next Steps

Once real FeneVision data arrives:
1. Replace `example_fenevision.csv` with actual export
2. Run mapper, check errors
3. Compare optimizer output to Joseph's actual routes
4. Feed back discrepancies — these are signals of missing constraints
