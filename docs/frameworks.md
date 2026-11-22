# Frameworks — ISO/IEC 25010:2023 and Azure Well-Architected

Each scorecard axis is mapped to its ISO/IEC 25010:2023 clause and its Azure Well-Architected pillar.
This mapping is provenance only: the score that gates produce is defined in
`config/scorecard.json` and `config/stages.json`.

## Rule

An axis without a mapping does not count toward framework conformance. If an axis is added to the
scorecard without a row here, it is excluded from ISO/WAF conformance (it may still be tracked
locally, but it cannot be claimed against either framework).

## Axis mapping

| Axis | ISO/IEC 25010:2023 clause | Azure Well-Architected pillar | Native mapping |
| --- | --- | --- | --- |
| security | Security | Security | yes |
| reliability | Reliability | Reliability | yes |
| performance | Performance efficiency | Performance Efficiency | yes |
| functional | Functional suitability | Operational Excellence | yes |
| maintainability | Maintainability | Operational Excellence | yes |
| compatibility | Compatibility | Performance Efficiency | yes |
| interaction | Usability / interaction capability | — | extended |
| safety | — | Reliability | extended |
| supplychain | — | Security | extended |
| flexibility | Flexibility | — | extended |
| handover | — | Operational Excellence | extended |

The five Azure Well-Architected pillars are: Cost Optimization, Operational Excellence,
Performance Efficiency, Reliability and Security.

Axes marked **extended** are project-defined extensions with no direct native ISO 25010:2023 clause
or Azure WAF pillar. They count in the local scorecard but do **not** count toward ISO/WAF
conformance per the rule above.

## Conformance statement

Autonoma's scorecard covers the nine characteristics of ISO/IEC 25010:2023 (functional suitability,
performance efficiency, compatibility, usability/interaction, reliability, security, maintainability,
flexibility, safety) plus supply chain and handover, and the five Azure Well-Architected pillars.
An axis that cannot be mapped to either framework does not count toward framework conformance.
