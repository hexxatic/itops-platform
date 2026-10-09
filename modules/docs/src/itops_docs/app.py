"""The docs Module object. Kept in its own file so models/service/views can import it
without importing each other in a cycle."""

from itops.sdk import Module

module = Module("itops_docs")
