from zbbx_mcp.formatters import (
    format_severity,
    format_host_list,
    format_problem_list,
    format_host_detail,
    format_hostgroup_list,
    _ts,
)


class TestSeverity:
    def test_known_severities(self):
        assert format_severity("0") == "Not classified"
        assert format_severity("3") == "Average"
        assert format_severity("5") == "Disaster"

    def test_unknown_severity(self):
        assert "Unknown" in format_severity("99")


class TestTimestamp:
    def test_valid_timestamp(self):
        result = _ts("1710720000")
        assert "2024" in result

    def test_invalid_timestamp(self):
        assert _ts("bad") == "bad"


class TestFormatHostList:
    def test_empty(self):
        assert format_host_list([]) == "No hosts found."

    def test_basic(self):
        hosts = [{"host": "web01", "name": "Web Server 01", "status": "0"}]
        result = format_host_list(hosts)
        assert "web01" in result
        assert "Enabled" in result

    def test_disabled_host(self):
        hosts = [{"host": "db01", "name": "DB", "status": "1"}]
        result = format_host_list(hosts)
        assert "Disabled" in result


class TestFormatProblemList:
    def test_empty(self):
        assert format_problem_list([]) == "No problems found."

    def test_basic(self):
        problems = [{
            "eventid": "123",
            "name": "CPU high",
            "severity": "4",
            "clock": "1710720000",
            "acknowledged": "0",
        }]
        result = format_problem_list(problems)
        assert "CPU high" in result
        assert "High" in result
        assert "123" in result

    def test_acknowledged(self):
        problems = [{
            "eventid": "1",
            "name": "test",
            "severity": "1",
            "clock": "0",
            "acknowledged": "1",
        }]
        result = format_problem_list(problems)
        assert "[ACK]" in result


class TestFormatHostDetail:
    def test_basic(self):
        host = {"host": "web01", "name": "Web 01", "hostid": "100", "status": "0"}
        result = format_host_detail(host)
        assert "# Host: web01" in result
        assert "100" in result

    def test_with_groups_and_interfaces(self):
        host = {
            "host": "web01",
            "name": "Web",
            "hostid": "1",
            "status": "0",
            "groups": [{"name": "Web servers"}],
            "interfaces": [{"type": "1", "ip": "10.0.0.1", "port": "10050"}],
        }
        result = format_host_detail(host)
        assert "Web servers" in result
        assert "Agent" in result
        assert "10.0.0.1" in result


class TestFormatHostgroupList:
    def test_empty(self):
        assert format_hostgroup_list([]) == "No host groups found."

    def test_basic(self):
        groups = [{"groupid": "1", "name": "Linux servers"}]
        result = format_hostgroup_list(groups)
        assert "Linux servers" in result
