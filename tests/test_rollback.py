from zbbx_mcp.rollback import RollbackLog, RollbackEntry, Action, SNAPSHOT_CONFIG


class TestRollbackLog:
    def test_empty_log(self):
        log = RollbackLog()
        assert len(log) == 0
        assert log.last is None
        assert log.pop_last() is None
        assert log.entries == []

    def test_record_and_retrieve(self):
        log = RollbackLog()
        log.record(Action.CREATE, "host", "123", {}, "Created host test")
        assert len(log) == 1
        assert log.last.object_id == "123"
        assert log.last.action == Action.CREATE
        assert log.last.object_type == "host"
        assert log.last.description == "Created host test"

    def test_record_update_with_snapshot(self):
        log = RollbackLog()
        snapshot = {"hostid": "456", "host": "web01", "status": "0"}
        log.record(Action.UPDATE, "host", "456", snapshot, "Updated host")
        assert log.last.snapshot == snapshot

    def test_record_delete_with_snapshot(self):
        log = RollbackLog()
        snapshot = {"triggerid": "789", "description": "CPU high", "priority": "4"}
        log.record(Action.DELETE, "trigger", "789", snapshot, "Deleted trigger")
        assert log.last.snapshot == snapshot

    def test_pop_last(self):
        log = RollbackLog()
        log.record(Action.CREATE, "host", "1", {}, "first")
        log.record(Action.CREATE, "host", "2", {}, "second")
        assert len(log) == 2

        entry = log.pop_last()
        assert entry.object_id == "2"
        assert len(log) == 1

        entry = log.pop_last()
        assert entry.object_id == "1"
        assert len(log) == 0

    def test_bounded_capacity(self):
        log = RollbackLog(max_entries=3)
        for i in range(5):
            log.record(Action.CREATE, "host", str(i), {}, f"host {i}")
        assert len(log) == 3
        # Oldest entries (0, 1) were evicted
        ids = [e.object_id for e in log.entries]
        assert ids == ["2", "3", "4"]

    def test_entries_order(self):
        log = RollbackLog()
        log.record(Action.CREATE, "host", "a", {}, "first")
        log.record(Action.UPDATE, "item", "b", {"name": "x"}, "second")
        log.record(Action.DELETE, "trigger", "c", {"desc": "y"}, "third")
        entries = log.entries
        assert len(entries) == 3
        assert entries[0].object_id == "a"
        assert entries[1].object_id == "b"
        assert entries[2].object_id == "c"

    def test_entry_summary(self):
        log = RollbackLog()
        entry = log.record(Action.CREATE, "host", "100", {}, "Created host test")
        summary = entry.summary
        assert "create" in summary
        assert "host" in summary
        assert "100" in summary


class TestSnapshotConfig:
    def test_host_config(self):
        assert "host" in SNAPSHOT_CONFIG
        cfg = SNAPSHOT_CONFIG["host"]
        assert cfg["id_field"] == "hostid"
        assert cfg["get_method"] == "host.get"
        assert cfg["create_method"] == "host.create"
        assert cfg["delete_method"] == "host.delete"

    def test_all_crud_types_configured(self):
        expected = {"host", "item", "trigger", "maintenance", "usermacro", "hostgroup"}
        assert set(SNAPSHOT_CONFIG.keys()) == expected

    def test_all_have_required_fields(self):
        required = {"get_method", "create_method", "update_method", "delete_method", "id_field"}
        for obj_type, cfg in SNAPSHOT_CONFIG.items():
            for field in required:
                assert field in cfg, f"{obj_type} missing {field}"
