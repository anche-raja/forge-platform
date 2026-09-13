package com.acme.orders.service;

public class CodeTable {
    public String describe() {
        return registry.lookup("0100") + registry.lookup("0500");
    }
    public Object jndi() throws Exception {
        return new javax.naming.InitialContext().lookup("java:comp/env/jms/orderQueue");
    }
}
