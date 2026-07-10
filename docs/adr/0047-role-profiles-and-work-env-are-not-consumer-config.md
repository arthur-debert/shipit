# Role Profiles and Work Env are Shipit-owned, not consumer-configured

**Role Profiles** are a fixed Shipit-owned registry and **Work Env** is inferred
from execution context, not declared per consumer repo. Consumer configuration
continues to describe project shape and policy — toolchains, lanes, reviewers,
artifacts, secrets — while Shipit owns the structural execution model: which
Role gets which Tree Profile, which Work Env shape follows, and which
enforcement applies.

The rejected alternative is a per-consumer role/work-env configuration surface.
That may become useful later, but today it would add complexity before the base
model is proven: consumers could weaken coordinator enforcement, route a Role
into the wrong checkout shape, or fork eval comparability across repos. We will
reconsider only after the fixed model is battle-hardened and real consumer
variation shows the configuration is worth its cost.
