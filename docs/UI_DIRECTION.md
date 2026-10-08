# UI direction

The current Applications page establishes the typography, dark slate palette, restrained teal accents, readable cards, connection states and responsive form. It is a functional foundation, not the final demo experience.

Final presentation goal: visitors can understand the application, see what failed, understand why AdaptiveNet acted, and see measured recovery without reading raw JSON.

Pages: Overview (regional health, evidence freshness, latency/error charts, latest action); Applications (endpoint/connection configuration); Experiments (bounded scenario controls + ground truth); Incidents (observation → classification → decision → confirmed result); Evaluation (baseline comparisons/export).

Keep the demo customer page separate from the operator dashboard. Use the same catalogue content before/after mitigation with region badge, request latency and honest failure states. Never draw fabricated live charts or label disconnected telemetry healthy. Move local simulator mechanics into a clearly named lab section when real integrations arrive.

Endpoint registration should be at most one small form for the research prototype. Guided AWS enrollment can later reuse the page/data model without changing the dashboard's main incident/experiment story.
