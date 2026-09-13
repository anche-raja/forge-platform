package com.acme.orders.config;

import javax.sql.DataSource;
import org.springframework.jdbc.datasource.lookup.JndiDataSourceLookup;

public class DataSourceConfig {
    public static final String DS_JNDI = "jdbc/ordersDS";
    public DataSource dataSource() {
        return new JndiDataSourceLookup().getDataSource(DS_JNDI);
    }
}
