package com.acme.orders.web;

import javax.servlet.annotation.WebFilter;

@WebFilter("/*")
public class AuditFilter implements javax.servlet.Filter {
}
