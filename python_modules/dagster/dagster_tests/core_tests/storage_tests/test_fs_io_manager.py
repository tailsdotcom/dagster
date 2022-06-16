import os
import pickle
import tempfile

import pytest

from dagster import (
    AssetKey,
    DailyPartitionsDefinition,
    MetadataValue,
    ModeDefinition,
    Out,
    Output,
    StaticPartitionsDefinition,
    execute_pipeline,
    graph,
    op,
    pipeline,
    solid,
)
from dagster.core.definitions.assets import (
    AssetGroup,
    AssetIn,
    asset,
    build_assets_job,
    multi_asset,
)
from dagster.core.definitions.version_strategy import VersionStrategy
from dagster.core.errors import DagsterInvariantViolationError
from dagster.core.execution.api import create_execution_plan
from dagster.core.instance import DagsterInstance
from dagster.core.storage.fs_io_manager import fs_io_manager
from dagster.core.test_utils import instance_for_test


def define_pipeline(io_manager):
    @solid
    def solid_a(_context):
        return [1, 2, 3]

    @solid
    def solid_b(_context, _df):
        return 1

    @pipeline(mode_defs=[ModeDefinition("local", resource_defs={"io_manager": io_manager})])
    def asset_pipeline():
        solid_b(solid_a())

    return asset_pipeline


def test_fs_io_manager():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager = fs_io_manager.configured({"base_dir": tmpdir_path})
        pipeline_def = define_pipeline(io_manager)

        result = execute_pipeline(pipeline_def)
        assert result.success

        handled_output_events = list(filter(lambda evt: evt.is_handled_output, result.event_list))
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, result.run_id, "solid_a", "result")
        result_metadata_entry_a = handled_output_events[0].event_specific_data.metadata_entries[0]
        assert result_metadata_entry_a.label == "path"
        assert result_metadata_entry_a.value == MetadataValue.path(filepath_a)
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.event_list))
        input_metadata_entry_a = loaded_input_events[0].event_specific_data.metadata_entries[0]
        assert input_metadata_entry_a.label == "path"
        assert input_metadata_entry_a.value == MetadataValue.path(filepath_a)
        assert len(loaded_input_events) == 1
        assert "solid_a" == loaded_input_events[0].event_specific_data.upstream_step_key

        filepath_b = os.path.join(tmpdir_path, result.run_id, "solid_b", "result")
        result_metadata_entry_b = handled_output_events[1].event_specific_data.metadata_entries[0]
        assert result_metadata_entry_b.label == "path"
        assert result_metadata_entry_b.value == MetadataValue.path(filepath_b)
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == 1


def test_fs_io_manager_base_dir():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        instance = DagsterInstance.ephemeral(tempdir=tmpdir_path)
        io_manager = fs_io_manager
        pipeline_def = define_pipeline(io_manager)

        result = execute_pipeline(pipeline_def, instance=instance)
        assert result.success
        assert result.result_for_solid("solid_a").output_value() == [1, 2, 3]

        with open(
            os.path.join(instance.storage_directory(), result.run_id, "solid_a", "result"),
            "rb",
        ) as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]


def test_fs_io_manager_memoization():
    recorder = []

    @op
    def my_op():
        recorder.append("entered")

    @graph
    def my_graph():
        my_op()

    class MyVersionStrategy(VersionStrategy):
        def get_solid_version(self, _):
            return "foo"

    with tempfile.TemporaryDirectory() as temp_dir:
        with instance_for_test(temp_dir=temp_dir) as instance:
            my_job = my_graph.to_job(version_strategy=MyVersionStrategy())

            unmemoized_plan = create_execution_plan(my_job, instance_ref=instance.get_ref())
            assert len(unmemoized_plan.step_keys_to_execute) == 1

            result = my_job.execute_in_process(instance=instance)
            assert result.success
            assert len(recorder) == 1

            execution_plan = create_execution_plan(my_job, instance_ref=instance.get_ref())
            assert len(execution_plan.step_keys_to_execute) == 0

            result = my_job.execute_in_process(instance=instance)
            assert result.success
            assert len(recorder) == 1


# lamdba functions can't be pickled (pickle.PicklingError)
l = lambda x: x * x


def test_fs_io_manager_unpicklable():
    @op
    def unpicklable_local_func_output():
        # locally defined functions can't be pickled (AttributeError)
        def local_func():
            return 1

        return local_func

    @op
    def unpicklable_lambda_output():
        return l

    @op
    def recursion_limit_output():
        # a will exceed the recursion limit of 1000 and can't be pickled (RecursionError)
        a = []
        for _ in range(2000):
            a = [a]
        return a

    @op
    def op_b(_i):
        return 1

    @graph
    def local_func_graph():
        op_b(unpicklable_local_func_output())

    @graph
    def lambda_graph():
        op_b(unpicklable_lambda_output())

    @graph
    def recursion_limit_graph():
        op_b(recursion_limit_output())

    with tempfile.TemporaryDirectory() as tmp_dir:
        with instance_for_test(temp_dir=tmp_dir) as instance:
            io_manager = fs_io_manager.configured({"base_dir": tmp_dir})

            local_func_job = local_func_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                DagsterInvariantViolationError, match=r"Object .* is not picklable. .*"
            ):
                local_func_job.execute_in_process(instance=instance)

            lambda_job = lambda_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                DagsterInvariantViolationError, match=r"Object .* is not picklable. .*"
            ):
                lambda_job.execute_in_process(instance=instance)

            recursion_job = recursion_limit_graph.to_job(resource_defs={"io_manager": io_manager})
            with pytest.raises(
                DagsterInvariantViolationError,
                match=r"Object .* exceeds recursion limit and is not picklable. .*",
            ):
                recursion_job.execute_in_process(instance=instance)


def get_assets_job(io_manager_def, partitions_def=None):
    asset1_key_prefix = ["one", "two", "three"]

    @asset(key_prefix=["one", "two", "three"], partitions_def=partitions_def)
    def asset1():
        return [1, 2, 3]

    @asset(
        key_prefix=["four", "five"],
        ins={"asset1": AssetIn(key_prefix=asset1_key_prefix)},
        partitions_def=partitions_def,
    )
    def asset2(asset1):
        return asset1 + [4]

    return build_assets_job(
        name="a", assets=[asset1, asset2], resource_defs={"io_manager": io_manager_def}
    )


def test_fs_io_manager_handles_assets():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        job_def = get_assets_job(io_manager_def)

        result = job_def.execute_in_process()
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, "one", "two", "three", "asset1")
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_node_events))
        assert len(loaded_input_events) == 1
        assert loaded_input_events[0].event_specific_data.upstream_step_key.endswith("asset1")

        filepath_b = os.path.join(tmpdir_path, "four", "five", "asset2")
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3, 4]


def test_fs_io_manager_partitioned():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})
        job_def = get_assets_job(
            io_manager_def, partitions_def=DailyPartitionsDefinition(start_date="2020-02-01")
        )

        result = job_def.execute_in_process(partition_key="2020-05-03")
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 2

        filepath_a = os.path.join(tmpdir_path, "one", "two", "three", "asset1", "2020-05-03")
        assert os.path.isfile(filepath_a)
        with open(filepath_a, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3]

        loaded_input_events = list(filter(lambda evt: evt.is_loaded_input, result.all_node_events))
        assert len(loaded_input_events) == 1
        assert loaded_input_events[0].event_specific_data.upstream_step_key.endswith("asset1")

        filepath_b = os.path.join(tmpdir_path, "four", "five", "asset2", "2020-05-03")
        assert os.path.isfile(filepath_b)
        with open(filepath_b, "rb") as read_obj:
            assert pickle.load(read_obj) == [1, 2, 3, 4]


def test_fs_io_manager_partitioned_multi_asset():
    with tempfile.TemporaryDirectory() as tmpdir_path:
        io_manager_def = fs_io_manager.configured({"base_dir": tmpdir_path})

        partitions = StaticPartitionsDefinition(["A"])

        @multi_asset(
            partitions_def=partitions,
            outs={
                "out_1": Out(asset_key=AssetKey("upstream_asset_1")),
                "out_2": Out(asset_key=AssetKey("upstream_asset_2")),
            },
        )
        def upstream_asset():
            return (Output(1, output_name="out_1"), Output(2, output_name="out_2"))

        @asset(
            partitions_def=partitions,
        )
        def downstream_asset(upstream_asset_1: int) -> int:
            del upstream_asset_1
            return 2

        group = AssetGroup(
            [upstream_asset, downstream_asset], resource_defs={"io_manager": io_manager_def}
        )

        job = group.build_job(name="TheJob")

        result = job.execute_in_process(partition_key="A")
        assert result.success

        handled_output_events = list(
            filter(lambda evt: evt.is_handled_output, result.all_node_events)
        )
        assert len(handled_output_events) == 3
