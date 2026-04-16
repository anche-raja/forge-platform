# Open Liberty Configuration Standards

<!-- This file is uploaded to S3 and ingested into the Bedrock Knowledge Base.
     FORGE uses it when generating or validating Liberty server.xml configurations
     and Dockerfile/container configurations for the migrated applications. -->

## Approved Feature Sets Per Application Type

<!-- TODO: Define which Liberty features are approved for each application type.
Example:

### REST API Applications
<featureManager>
  <feature>restfulWS-3.1</feature>       <!-- JAX-RS 3.1 -->
  <feature>jsonb-3.0</feature>            <!-- JSON-B -->
  <feature>jsonp-2.1</feature>            <!-- JSON-P -->
  <feature>cdi-4.0</feature>             <!-- CDI -->
  <feature>mpHealth-4.0</feature>         <!-- Health check endpoint -->
  <feature>mpMetrics-5.0</feature>        <!-- Metrics endpoint -->
  <feature>jwtSso-1.0</feature>           <!-- JWT SSO -->
</featureManager>

### Full Web Applications (with JSP/JSF)
<featureManager>
  <feature>servlet-6.0</feature>
  <feature>pages-3.1</feature>           <!-- JSP -->
  <feature>faces-4.0</feature>           <!-- JSF / Jakarta Faces -->
  <feature>cdi-4.0</feature>
  <feature>jpa-3.1</feature>
</featureManager>
-->

## Datasource Configuration Patterns

<!-- TODO: Define the approved datasource configuration.
Example:

### DB2 Datasource
<dataSource id="db2DS" jndiName="jdbc/appDS">
  <jdbcDriver libraryRef="DB2Lib"/>
  <properties.db2.jcc
    databaseName="${env.DB_NAME}"
    serverName="${env.DB_HOST}"
    portNumber="${env.DB_PORT}"
    user="${env.DB_USER}"
    password="${env.DB_PASSWORD}"/>
  <connectionManager
    minPoolSize="5"
    maxPoolSize="50"
    connectionTimeout="30s"
    maxIdleTime="10m"/>
</dataSource>

### Connection Pool Sizing Guidelines
- Dev: minPoolSize=2, maxPoolSize=10
- Staging: minPoolSize=5, maxPoolSize=30
- Prod: minPoolSize=10, maxPoolSize=50 (tune based on ECS task count)
-->

## ECS Resource Allocation Guidelines

<!-- TODO: Define ECS task definition resource allocations per application tier.
Example:

### Tier 1 — Lightweight REST APIs (no DB, stateless)
CPU: 256 (.25 vCPU)
Memory: 512 MB
JVM heap: -Xms128m -Xmx384m

### Tier 2 — Standard Application Services (DB-connected)
CPU: 512 (.5 vCPU)
Memory: 1024 MB
JVM heap: -Xms256m -Xmx768m

### Tier 3 — High-Throughput / Batch Services
CPU: 1024 (1 vCPU)
Memory: 2048 MB
JVM heap: -Xms512m -Xmx1536m

### Health Check Configuration
Path: /health (mpHealth endpoint)
Interval: 30s
Timeout: 5s
Retries: 3
Start period: 60s (allow JVM warm-up)
-->

## server.xml Template

<!-- TODO: Paste your baseline server.xml template here. -->
