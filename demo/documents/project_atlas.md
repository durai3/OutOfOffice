# Project Atlas - Status and Facts

Project Atlas is Northwind Analytics' migration of customers from the legacy reporting stack to the new Atlas data platform. {{name}} is the executive sponsor.

## Current status (as of the last weekly review)
- Overall progress: 68 percent complete. Status is on track.
- The private beta is scheduled for 14 November with 5 design-partner customers.
- General availability is planned for the end of Q1 next year.
- 31 of the 46 legacy reports have been rebuilt and validated against production numbers.

## Team
- The Atlas squad has 12 people: 7 engineers, 2 data engineers, 1 designer, 1 QA engineer and 1 product manager.
- Engineering lead: Priya Raman. Product manager: Jonas Weber. Data engineering lead: Mei Tanaka.

## Technology
- Primary database: PostgreSQL 16 on managed cloud infrastructure.
- Event streaming: Apache Kafka. Transformations are written in dbt.
- Backend services are written in Python with FastAPI. The web front end is React with TypeScript.
- The platform runs on Kubernetes and is deployed through a continuous delivery pipeline with automated rollback.

## Risks and open items
- Risk 1: the validation of the 15 remaining legacy reports depends on access to two customers' historical data. Access is expected within two weeks.
- Risk 2: the on-call rota for the beta period is not yet staffed. Engineering lead Priya Raman is drafting it.
- Open item: a decision on the data retention period for beta customers is pending with the legal team.

## Metrics
- Median dashboard load time on Atlas: 1.4 seconds, against 6.2 seconds on the legacy stack.
- Beta success target: 4 of the 5 design partners running their weekly review on Atlas by the end of the beta.
