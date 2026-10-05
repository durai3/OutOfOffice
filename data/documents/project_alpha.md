# Project Alpha

## Overview

Project Alpha is the customer analytics platform that replaces the legacy reporting stack.
The programme is led by Durai Kumaran, with Sarah as engineering lead and Mike as product owner.
It serves the enterprise tier first and the self-serve tier one quarter later.

## Architecture

Project Alpha uses PostgreSQL 16 on Amazon RDS as its primary database, with Redis 7 for caching and session storage.
The backend is written in Python 3.12 using FastAPI and runs as containers on AWS ECS Fargate behind an Application Load Balancer.
Events flow through Amazon Kinesis into a dbt-managed warehouse on Snowflake for analytics workloads.
The web client is a React 19 single-page application served from CloudFront.

## Tech stack

| Layer      | Choice                              |
| ---------- | ----------------------------------- |
| Database   | PostgreSQL 16 (Amazon RDS)          |
| Cache      | Redis 7                             |
| Backend    | Python 3.12, FastAPI, ECS Fargate   |
| Streaming  | Amazon Kinesis                      |
| Warehouse  | Snowflake with dbt                  |
| Frontend   | React 19 on CloudFront              |

## Deployment timeline

- User acceptance testing starts on 29 September 2026 and runs for two weeks.
- A code freeze begins on 7 October 2026; only critical fixes are merged after that date.
- Production go-live for Project Alpha is scheduled for Tuesday 14 October 2026.
- The self-serve tier follows in January 2027.

## Security

The security review is owned by the platform team and must be completed before the production deployment.
Secrets are stored in AWS Secrets Manager and rotated every 90 days.
All customer data is encrypted at rest with AWS KMS and in transit with TLS 1.3.
