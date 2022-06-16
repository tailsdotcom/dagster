import pytest

from dagster import (
    AssetKey,
    AssetSelection,
    AssetsDefinition,
    DagsterEventType,
    DailyPartitionsDefinition,
    EventRecordsFilter,
    HourlyPartitionsDefinition,
    IOManager,
    Out,
    Output,
    SourceAsset,
    define_asset_job,
    graph,
    in_process_executor,
    io_manager,
    op,
    repository,
    schedule_from_partitions,
)
from dagster._check import CheckError
from dagster.core.definitions.assets import asset, multi_asset
from dagster.core.definitions.load_assets_from_modules import prefix_assets
from dagster.core.errors import DagsterInvalidDefinitionError, DagsterInvalidSubsetError
from dagster.core.execution.with_resources import with_resources
from dagster.core.test_utils import instance_for_test


def _all_asset_keys(result):
    mats = [
        event.event_specific_data.materialization
        for event in result.all_events
        if event.event_type_value == "ASSET_MATERIALIZATION"
    ]
    ret = {mat.asset_key for mat in mats}
    assert len(mats) == len(ret)
    return ret


def asset_aware_io_manager():
    class MyIOManager(IOManager):
        def __init__(self):
            self.db = {}

        def handle_output(self, context, obj):
            self.db[context.asset_key] = obj

        def load_input(self, context):
            return self.db.get(context.asset_key)

    io_manager_obj = MyIOManager()

    @io_manager
    def _asset_aware():
        return io_manager_obj

    return io_manager_obj, _asset_aware


def _get_assets_defs(use_multi: bool = False, allow_subset: bool = False):
    """
    Dependencies:
        "upstream": {
            "start": set(),
            "a": {"start"},
            "b": set(),
            "c": {"b"},
            "d": {"a", "b"},
            "e": {"c"},
            "f": {"e", "d"},
            "final": {"a", "d"},
        },
        "downstream": {
            "start": {"a"},
            "b": {"c", "d"},
            "a": {"final", "d"},
            "c": {"e"},
            "d": {"final", "f"},
            "e": {"f"},
        }
    """

    @asset
    def start():
        return 1

    @asset
    def a(start):
        return start + 1

    @asset
    def b():
        return 1

    @asset
    def c(b):
        return b + 1

    @multi_asset(
        outs={
            "a": Out(is_required=False),
            "b": Out(is_required=False),
            "c": Out(is_required=False),
        },
        internal_asset_deps={
            "a": {AssetKey("start")},
            "b": set(),
            "c": {AssetKey("b")},
        },
        can_subset=allow_subset,
    )
    def abc_(context, start):
        a = (start + 1) if start else None
        b = 1
        c = b + 1
        out_values = {"a": a, "b": b, "c": c}
        outputs_to_return = context.selected_output_names if allow_subset else "abc"
        for output_name in outputs_to_return:
            yield Output(out_values[output_name], output_name)

    @asset
    def d(a, b):
        return a + b

    @asset
    def e(c):
        return c + 1

    @asset
    def f(d, e):
        return d + e

    @multi_asset(
        outs={
            "d": Out(is_required=False),
            "e": Out(is_required=False),
            "f": Out(is_required=False),
        },
        internal_asset_deps={
            "d": {AssetKey("a"), AssetKey("b")},
            "e": {AssetKey("c")},
            "f": {AssetKey("d"), AssetKey("e")},
        },
        can_subset=allow_subset,
    )
    def def_(context, a, b, c):
        d = (a + b) if a and b else None
        e = (c + 1) if c else None
        f = (d + e) if d and e else None
        out_values = {"d": d, "e": e, "f": f}
        outputs_to_return = context.selected_output_names if allow_subset else "def"
        for output_name in outputs_to_return:
            yield Output(out_values[output_name], output_name)

    @asset
    def final(a, d):
        return a + d

    if use_multi:
        return [start, abc_, def_, final]
    return [start, a, b, c, d, e, f, final]


@pytest.mark.parametrize(
    "job_selection,use_multi,expected_error",
    [
        ("*", False, None),
        ("*", True, None),
        ("e", False, None),
        ("e", True, (DagsterInvalidSubsetError, "")),
        (
            "x",
            False,
            (DagsterInvalidSubsetError, r"AssetKey\(s\) {'x'} were selected"),
        ),
        (
            "x",
            True,
            (DagsterInvalidSubsetError, r"AssetKey\(s\) {'x'} were selected"),
        ),
        (
            ["start", "x"],
            False,
            (DagsterInvalidSubsetError, r"AssetKey\(s\) {'x'} were selected"),
        ),
        (
            ["start", "x"],
            True,
            (DagsterInvalidSubsetError, r"AssetKey\(s\) {'x'} were selected"),
        ),
        (["d", "e", "f"], False, None),
        (["d", "e", "f"], True, None),
        (["start+"], False, None),
        (
            ["start+"],
            True,
            (
                DagsterInvalidSubsetError,
                r"When building job, the AssetsDefinition 'abc_' contains asset keys "
                r"\[AssetKey\(\['a'\]\), AssetKey\(\['b'\]\), AssetKey\(\['c'\]\)\], but attempted to "
                r"select only \[AssetKey\(\['a'\]\)\]",
            ),
        ),
    ],
)
def test_resolve_subset_job_errors(job_selection, use_multi, expected_error):
    job_def = define_asset_job(name="some_name", selection=job_selection)
    if expected_error:
        expected_class, expected_message = expected_error
        with pytest.raises(expected_class, match=expected_message):
            job_def.resolve(assets=_get_assets_defs(use_multi), source_assets=[])
    else:
        assert job_def.resolve(assets=_get_assets_defs(use_multi), source_assets=[])


@pytest.mark.parametrize(
    "job_selection,expected_assets",
    [
        (None, "a,b,c"),
        ("a+", "a,b"),
        ("+c", "b,c"),
        (["a", "c"], "a,c"),
        (AssetSelection.keys("a", "c") | AssetSelection.keys("c", "b"), "a,b,c"),
    ],
)
def test_simple_graph_backed_asset_subset(job_selection, expected_assets):
    @op
    def one():
        return 1

    @op
    def add_one(x):
        return x + 1

    @op(out=Out(io_manager_key="asset_io_manager"))
    def create_asset(x):
        return x * 2

    @graph
    def a():
        return create_asset(add_one(add_one(one())))

    @graph
    def b(a):
        return create_asset(add_one(add_one(a)))

    @graph
    def c(b):
        return create_asset(add_one(add_one(b)))

    a_asset = AssetsDefinition.from_graph(a)
    b_asset = AssetsDefinition.from_graph(b)
    c_asset = AssetsDefinition.from_graph(c)

    _, io_manager_def = asset_aware_io_manager()
    final_assets = with_resources([a_asset, b_asset, c_asset], {"asset_io_manager": io_manager_def})

    # run once so values exist to load from
    define_asset_job("initial").resolve(final_assets, source_assets=[]).execute_in_process()

    # now build the subset job
    job = define_asset_job("asset_job", selection=job_selection).resolve(
        final_assets, source_assets=[]
    )

    result = job.execute_in_process()

    expected_asset_keys = set((AssetKey(a) for a in expected_assets.split(",")))

    # make sure we've generated the correct set of keys
    assert _all_asset_keys(result) == expected_asset_keys

    if AssetKey("a") in expected_asset_keys:
        # (1 + 1 + 1) * 2
        assert result.output_for_node("a.create_asset") == 6
    if AssetKey("b") in expected_asset_keys:
        # (6 + 1 + 1) * 8
        assert result.output_for_node("b.create_asset") == 16
    if AssetKey("c") in expected_asset_keys:
        # (16 + 1 + 1) * 2
        assert result.output_for_node("c.create_asset") == 36


@pytest.mark.parametrize("use_multi", [True, False])
@pytest.mark.parametrize(
    "job_selection,expected_assets,prefixes",
    [
        ("*", "start,a,b,c,d,e,f,final", None),
        ("a", "a", None),
        ("b+", "b,c,d", None),
        ("+f", "f,d,e", None),
        ("++f", "f,d,e,c,a,b", None),
        ("start*", "start,a,d,f,final", None),
        (["+a", "b+"], "start,a,b,c,d", None),
        (["*c", "final"], "b,c,final", None),
        ("*", "start,a,b,c,d,e,f,final", ["core", "models"]),
        ("core/models/a", "a", ["core", "models"]),
        ("core/models/b+", "b,c,d", ["core", "models"]),
        ("+core/models/f", "f,d,e", ["core", "models"]),
        ("++core/models/f", "f,d,e,c,a,b", ["core", "models"]),
        ("core/models/start*", "start,a,d,f,final", ["core", "models"]),
        (["+core/models/a", "core/models/b+"], "start,a,b,c,d", ["core", "models"]),
        (["*core/models/c", "core/models/final"], "b,c,final", ["core", "models"]),
        (AssetSelection.all(), "start,a,b,c,d,e,f,final", None),
        (AssetSelection.keys("a", "b", "c"), "a,b,c", None),
        (AssetSelection.keys("f").upstream(depth=1), "f,d,e", None),
        (AssetSelection.keys("f").upstream(depth=2), "f,d,e,c,a,b", None),
        (AssetSelection.keys("start").downstream(), "start,a,d,f,final", None),
        (
            AssetSelection.keys("a").upstream(depth=1)
            | AssetSelection.keys("b").downstream(depth=1),
            "start,a,b,c,d",
            None,
        ),
        (AssetSelection.keys("c").upstream() | AssetSelection.keys("final"), "b,c,final", None),
        (AssetSelection.all(), "start,a,b,c,d,e,f,final", ["core", "models"]),
        (
            AssetSelection.keys("core/models/a").upstream(depth=1)
            | AssetSelection.keys("core/models/b").downstream(depth=1),
            "start,a,b,c,d",
            ["core", "models"],
        ),
    ],
)
def test_define_selection_job(job_selection, expected_assets, use_multi, prefixes):

    _, io_manager_def = asset_aware_io_manager()
    # for these, if we have multi assets, we'll always allow them to be subset
    prefixed_assets = _get_assets_defs(use_multi=use_multi, allow_subset=use_multi)
    # apply prefixes
    for prefix in reversed(prefixes or []):
        prefixed_assets = prefix_assets(prefixed_assets, prefix)

    final_assets = with_resources(
        prefixed_assets,
        resource_defs={"io_manager": io_manager_def},
    )

    # run once so values exist to load from
    define_asset_job("initial").resolve(final_assets, source_assets=[]).execute_in_process()

    # now build the subset job
    job = define_asset_job("asset_job", selection=job_selection).resolve(
        final_assets, source_assets=[]
    )

    with instance_for_test() as instance:
        result = job.execute_in_process(instance=instance)
        planned_asset_keys = {
            record.event_log_entry.dagster_event.event_specific_data.asset_key
            for record in instance.get_event_records(
                EventRecordsFilter(DagsterEventType.ASSET_MATERIALIZATION_PLANNED)
            )
        }

    expected_asset_keys = set(
        (AssetKey([*(prefixes or []), a]) for a in expected_assets.split(","))
    )
    # make sure we've planned on the correct set of keys
    assert planned_asset_keys == expected_asset_keys

    # make sure we've generated the correct set of keys
    assert _all_asset_keys(result) == expected_asset_keys

    if use_multi:
        expected_outputs = {
            "start": 1,
            "abc_.a": 2,
            "abc_.b": 1,
            "abc_.c": 2,
            "def_.d": 3,
            "def_.e": 3,
            "def_.f": 6,
            "final": 5,
        }
    else:
        expected_outputs = {"start": 1, "a": 2, "b": 1, "c": 2, "d": 3, "e": 3, "f": 6, "final": 5}

    # check if the output values are as we expect
    for output, value in expected_outputs.items():
        asset_name = output.split(".")[-1]
        if asset_name in expected_assets.split(","):
            # dealing with multi asset
            if output != asset_name:
                assert result.output_for_node(output.split(".")[0], asset_name)
            # dealing with regular asset
            else:
                assert result.output_for_node(output, "result") == value


def test_source_asset_selection():
    @asset
    def a(source):
        return source + 1

    @asset
    def b(a):
        return a + 1

    assert define_asset_job("job", selection="*b").resolve(
        assets=[a, b], source_assets=[SourceAsset("source")]
    )


def test_source_asset_selection_missing():
    @asset
    def a(source):
        return source + 1

    @asset
    def b(a):
        return a + 1

    with pytest.raises(DagsterInvalidDefinitionError, match="sources"):
        define_asset_job("job", selection="*b").resolve(assets=[a, b], source_assets=[])


@asset
def foo():
    return 1


@pytest.mark.skip()
def test_executor_def():
    job = define_asset_job("with_exec", executor_def=in_process_executor).resolve([foo], [])
    assert job.executor_def == in_process_executor  # pylint: disable=comparison-with-callable


def test_tags():
    my_tags = {"foo": "bar"}
    job = define_asset_job("with_tags", tags=my_tags).resolve([foo], [])
    assert job.tags == my_tags


def test_description():
    description = "Some very important description"
    job = define_asset_job("with_tags", description=description).resolve([foo], [])
    assert job.description == description


def _get_partitioned_assets(partitions_def):
    @asset(partitions_def=partitions_def)
    def a():
        return 1

    @asset(partitions_def=partitions_def)
    def b(a):
        return a + 1

    @asset(partitions_def=partitions_def)
    def c(b):
        return b + 1

    return [a, b, c]


def test_config():
    @asset
    def foo():
        return 1

    @asset(config_schema={"val": int})
    def config_asset(context, foo):
        return foo + context.op_config["val"]

    @asset(config_schema={"val": int})
    def other_config_asset(context, config_asset):
        return config_asset + context.op_config["val"]

    job = define_asset_job(
        "config_job",
        config={
            "ops": {
                "config_asset": {"config": {"val": 2}},
                "other_config_asset": {"config": {"val": 3}},
            }
        },
    ).resolve(assets=[foo, config_asset, other_config_asset], source_assets=[])

    result = job.execute_in_process()

    assert result.output_for_node("other_config_asset") == 1 + 2 + 3


def test_simple_partitions():
    partitions_def = HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = define_asset_job("hourly", partitions_def=partitions_def).resolve(
        _get_partitioned_assets(partitions_def), []
    )
    assert job.partitions_def == partitions_def


def test_partitioned_schedule():
    partitions_def = HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = define_asset_job("hourly", partitions_def=partitions_def)

    schedule = schedule_from_partitions(job)

    spd = schedule.get_partition_set()._partitions_def  # pylint: disable=protected-access
    assert spd == partitions_def


def test_partitioned_schedule_on_repo():
    partitions_def = HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = define_asset_job("hourly", partitions_def=partitions_def)

    schedule = schedule_from_partitions(job)

    @repository
    def my_repo():
        return [
            job,
            schedule,
            *_get_partitioned_assets(partitions_def),
        ]

    assert my_repo()


def test_intersecting_partitions_on_repo_invalid():
    partitions_def = HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    job = define_asset_job("hourly", partitions_def=partitions_def)

    schedule = schedule_from_partitions(job)

    @asset(partitions_def=DailyPartitionsDefinition(start_date="2020-01-01"))
    def d(c):
        return c

    with pytest.raises(CheckError, match="partitions_def of Daily"):

        @repository
        def my_repo():
            return [
                job,
                schedule,
                *_get_partitioned_assets(partitions_def),
                d,
            ]


def test_intersecting_partitions_on_repo_valid():
    partitions_def = HourlyPartitionsDefinition(start_date="2020-01-01-00:00")
    partitions_def2 = DailyPartitionsDefinition(start_date="2020-01-01")
    job = define_asset_job("hourly", partitions_def=partitions_def, selection="a++")
    job2 = define_asset_job("daily", partitions_def=partitions_def2, selection="d")

    schedule = schedule_from_partitions(job)
    schedule2 = schedule_from_partitions(job2)

    @asset(partitions_def=partitions_def2)
    def d(c):
        return c

    @repository
    def my_repo():
        return [
            job,
            schedule,
            schedule2,
            *_get_partitioned_assets(partitions_def),
            d,
        ]

    assert my_repo
