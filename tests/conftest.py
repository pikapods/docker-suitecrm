def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "runtime: tests that boot the container against a real database "
        "(slow; require docker daemon). Skip with `-m 'not runtime'`.",
    )
