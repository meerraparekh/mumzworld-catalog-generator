# Mumzworld Catalog Generator

Turns vendor product data into validated, reviewable
**Magento 2** and **NetSuite** import files for Mumzworld. It does the cataloging itself: it reads the vendor
file, checks the NetSuite master, links images, assigns categories, maps attributes to approved dropdown values,
writes titles and descriptions, maps both target templates, raises vendor queries, merges vendor responses and
validates everything. A person approves the result before anything is exported.

```bash
pip install -r requirements.txt
python run.py                 # http://127.0.0.1:5050
python -m mumzworld_catalog.samples demo_files   # optional: made-up sample files to try it with
```

Data is kept under `data/` (set `MUMZ_DATA_DIR` to move it). Tickets, the reference library and
approved rules stay there between sessions. A ticket can also be exported as a ZIP package and imported elsewhere.

## First run: reference library

The app applies **only** what is in the uploaded reference files. When a file is missing, the checks that need it are
blocked; nothing is filled in by guesswork. Upload these on the **Reference library** page:

| Kind | Read as |
|---|---|
| Approved categories | `Level 1/2/3…` or `Category/Subcategory` columns, or a `Path` column. Optional `Category ID`, `Keywords`, `Special`, `Allow multiple` |
| Attribute dropdowns | `Attribute / Value (/ Option ID)` rows, or one column per attribute |
| Mapping & content rules workbook | `Mappings`, `Attributes`, `ContentRules`, `ValueMap`, `Settings` sheets. **Download a starter** that already lists every template column |
| Magento 2 / NetSuite templates | `.xlsx`/`.xlsm`/`.csv`. Header row, required markers and drop-downs are detected. The output is written into a copy of the template |
| NetSuite product master | SKU (`Item Name/Number`…), internal ID, UPC/barcode, vendor SKU columns |
| Team review sheets | `SKU / Field / Original / Corrected / Comment / Reason / Approval Status / Category / Brand / Date` |
| SOP, completed tickets | Kept for reference and used as context for the optional assistant. SOP rules apply only once they are written into the rules workbook |

**Setup check** lists conflicts, for example:
- a mapping row for a column the template doesn't have
- an approved list that doesn't exist
- a ValueMap target that is not an approved value
- a template drop-down that disagrees with the approved list
- a required template column with no mapping

### Large files

A NetSuite master over 20 MB is read once, in the background, as soon as it is uploaded. Only the columns used for
matching are kept, from every tab that has a SKU column. The ticket page shows how many rows have been read and
refreshes itself, and processing starts once reading is done. Every parsed reference file is cached by its contents
under `data/cache/`, so it is never read again, even after a restart. On a test machine a 600,000-row, two-tab
master (58 MB) took about 70 seconds; a 300 MB master takes a few minutes, once. Exporting the master as CSV is
also supported, and is read the same way.

## Ticket workflow

1. **Create ticket.** Enter the number, vendor, region, date and instructions, and upload the vendor file(s),
   images/ZIPs, documents and any ticket-specific reference files. The inventory shows each file's sheets, record and
   SKU counts, and lists missing or unreadable files. Vendor columns are mapped to internal fields from their header
   names, and you can change the mapping before processing.
2. **Process.** This step (re-runnable at any time, keeping all reviewer work):
   - normalizes values safely and keeps the original next to each one, with the transformation and its reason
   - keeps barcodes as text and never "fixes" them: invalid check digits, scientific notation and lost leading zeros
     are flagged instead
   - matches against NetSuite: an exact SKU match is excluded as existing, a barcode or vendor-SKU match is held for
     investigation, and a SKU match with a different barcode is a conflict
   - associates images on clear filename evidence only; anything else is marked uncertain
   - **categorizes**, in this order: approved rules, then a vendor value that is itself an approved category, then
     evidence scoring against category names and keywords, then the optional assistant. Ties go to manual review
   - **maps attributes**, in this order: approved rules, ValueMap, then exact or spacing-insensitive matches to
     approved values. Anything else is flagged, never forced
   - builds content from the `ContentRules` (title pattern, case, length limits, bullets) and flags claim words that
     are not in the vendor's own data
   - fills every Magento 2 and NetSuite column from the `Mappings` sheet

   Every field records its decision class: *Directly sourced, Rule-based, Mapped, Cataloging judgment, Vendor query
   required, Manual review required* or *Reviewer set*.
3. **Working catalog and vendor queries** are produced together. Queries are only raised for required information
   that is missing, unsupported or contradictory: one row per SKU and field, plus a consolidated sheet per SKU and
   the allowed values.
4. **Responses.** Upload as many files as needed, either the query sheet with *Vendor Answer* filled in or any sheet
   with a SKU column. Answers are merged by SKU and field:
   - reviewer-locked values are never overwritten
   - a different answer to an already-answered field is a conflict
   - unknown or ambiguous SKUs go to the exceptions list

   Each import shows SKUs matched, fields updated, queries resolved, partially resolved and outstanding, conflicts,
   unmatched SKUs and new issues.
5. **Validation.** Product, content, Magento 2, NetSuite and cross-system checks, each graded *Blocker*, *Warning* or
   *Info*, with the current value, the requirement, the reason and a suggested action. Blockers can be waived only
   with a reason, and the waiver is recorded with who and when.
6. **Review and export.**
   - On the product page, the original and working values sit side by side. You can edit, accept or unlock a value,
     record a match decision, link or unlink images and record references.
   - Bulk approval shows a preview first.
   - Final approval needs an authorized reviewer (Settings). It is withdrawn by any later change.
   - Export writes the two import files with the template's exact columns and sheets, plus the supporting reports:
     validation and exceptions, existing-product exclusions, outstanding queries, source-to-target mapping and
     transformations, review and change history, potential new rules, and the references used.

## Rules learned from reviews

Review sheets and reviewer corrections are grouped into lessons, each classified as one of:
- general rule candidate
- category-specific rule candidate
- brand-specific rule candidate
- one-off correction
- conflicting example

Candidates can be stored as **suggestions**. A suggestion affects products only after someone approves it on the
**Approved rules** page, and each value it sets names the rule (`rule:R0003`).

## Optional assistant and Mumzworld.com lookup

Both are off by default (Settings).
- **The assistant** (Claude, `claude-opus-5-5`, via the `anthropic` package and `ANTHROPIC_API_KEY`) is asked only for
  judgment calls: choosing among approved categories, finding an approved value with the same meaning, drafting
  descriptions from vendor evidence, and describing images. Its answers are checked against the approved lists and
  always need review. Requests use server-side model fallbacks.
- **The Mumzworld.com lookup** records what the search page returned. If the page cannot be reached, it records that
  instead of claiming a search.

## Tests

`pytest tests/test_mumzworld.py` runs the whole flow on the sample files. A fake assistant stands in for the API, so
the tests don't use the network.
