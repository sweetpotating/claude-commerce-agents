# Shopping-agent skills (products first)

A copy of `vendor/commerce-agents/shopping-agent/skills/` at the vendored commit, with the
rules that held products back until the shopper answered questions changed so the first
reply always recommends products:

- `purchase-research`: no intake questions; open facts become stated assumptions and
  refinement chips, and a broad ask gets criteria and options in one turn.
- `planning-goals`: no product-less outline turn; the likeliest outline is filled in, and
  the other goes in a chip.
- `search-discovery`: missing dates, sizes, or recipients no longer block results; the
  likeliest answer is assumed and the rest offered as chips.

`customer-care` and `memory-personalization` are unchanged. When moving `VENDOR_COMMIT`,
diff the vendor skills against these and carry their changes over.
