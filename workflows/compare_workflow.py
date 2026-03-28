from __future__ import annotations

# Placeholder for comparison workflow (Week 3)
# Will implement: fan_out_search -> extract_claims -> compare -> format
#
# LangGraph fan-out pattern:
#   START (topic)
#     -> search_reuters, search_bbc, search_ap  (parallel)
#     -> extract_claims
#     -> compare_synthesize
#     -> format_response
#     -> END
