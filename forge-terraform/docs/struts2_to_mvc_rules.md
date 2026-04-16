# Struts 2 to Spring MVC Migration Rules — Project Specific

<!-- This file is uploaded to S3 and ingested into the Bedrock Knowledge Base.
     Add your project-specific edge cases and known patterns here.
     FORGE agents use this to handle situations the generic migration rules don't cover. -->

## Known Edge Cases in This Codebase

<!-- TODO: Document Struts 2 patterns in your codebase that require special handling.
Example:
- Several actions extend BaseSecureAction — map to Spring @Controller extending BaseController
- OrderAction has dual use as both a CRUD action and a workflow state machine — split into
  OrderController (CRUD) and OrderWorkflowController (state transitions) post-migration
- ReportAction uses OGNL to dynamically invoke method names from request params —
  replace with a switch/dispatch pattern in Spring MVC
-->

## Custom Interceptors and Their Spring Equivalents

<!-- TODO: List your custom Struts 2 interceptors and how they should be mapped.
Example:
| Struts 2 Interceptor       | Spring MVC Equivalent             |
|----------------------------|-----------------------------------|
| AuthenticationInterceptor  | JwtAuthenticationFilter (existing)|
| AuditLogInterceptor        | AuditLogHandlerInterceptor        |
| TenantContextInterceptor   | TenantContextFilter               |
| ValidationInterceptor      | @Valid + GlobalExceptionHandler   |
-->

## Custom Result Types

<!-- TODO: Document any custom Struts 2 result types in use.
Example:
- JsonResult: handled by @RestController + Jackson (no special mapping needed)
- PdfResult: map to ResponseEntity<byte[]> with MediaType.APPLICATION_PDF
- StreamResult: map to ResponseEntity<StreamingResponseBody>
- TileResult (Apache Tiles): migrate to Thymeleaf or keep Tiles with Spring MVC adapter
-->

## Known OGNL Patterns and Their Spring Equivalents

<!-- TODO: List OGNL expressions used in JSPs or action properties that need attention.
Example:
- <s:property value="order.customer.address.city"/> → Thymeleaf: ${order.customer.address.city}
- <s:iterator value="items" var="item"> → th:each="item : ${items}"
- <s:if test="user.admin"> → th:if="${user.admin}"
- OGNL type conversion: replace ValueStack-based conversion with Spring's ConversionService
-->

## struts.xml Action Mapping Reference

<!-- TODO: Paste the relevant sections of your struts.xml here so FORGE can map
     action names and result paths to Spring @RequestMapping annotations.
Example:
<package name="orders" namespace="/orders" extends="json-default">
  <action name="create" class="com.corp.orders.CreateOrderAction" method="execute">
    <result name="success" type="json"/>
    <result name="input">/WEB-INF/views/orders/create.jsp</result>
  </action>
</package>
-->
