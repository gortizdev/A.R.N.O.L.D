"""addons.py: working features merged into a sculpture's mesh.

Real meshes and real booleans (manifold3d): the feature has to be there,
the right size, and the part still one solid afterwards.
"""

import shutil

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("manifold3d")

from arnold import addons

from test_adjust import bench, record  # noqa: F401 - bench is a fixture


def figure(path, extents=(30, 24, 50)):
    """A figure-ish blob with a flat bottom, standing on z=0."""
    blob = trimesh.creation.icosphere(subdivisions=4)
    blob.apply_scale(np.array(extents) / blob.extents)
    cut = trimesh.creation.box(extents=[200, 200, 100])
    cut.apply_translation([0, 0, -50 - extents[2] * 0.35])
    blob = trimesh.boolean.difference([blob, cut], engine="manifold")
    blob.apply_translation([0, 0, -blob.bounds[0][2]])
    blob.export(str(path))
    return path


class TestParse:
    def test_words(self):
        found = addons.parse("plinth, magnet 8x3, keyring, hollow open top")
        assert found.names() == ["plinth", "magnet", "keyring", "hollow"]
        assert found.magnet == {"d": 8.0, "h": 3.0} and found.hollow["open"] == "top"

    def test_json(self):
        found = addons.parse({"keyring": {"hole": 6}, "plinth": False})
        assert found.names() == ["keyring"] and found.keyring["hole"] == 6.0

    def test_unknown_is_refused_in_words(self):
        with pytest.raises(addons.AddonError, match="plinth, a magnet pocket"):
            addons.parse("a jetpack")

    @pytest.mark.parametrize("prompt, names", [
        ("a dragon keychain", ["keyring"]),
        ("a frog planter", ["hollow"]),
        ("an owl on a plinth", ["plinth"]),
        ("an owl on a branch", []),
    ])
    def test_implied_by_the_request(self, prompt, names):
        assert addons.implied(prompt).names() == names

    @pytest.mark.parametrize("change, names", [
        ("add a key ring loop", ["keyring"]),
        ("hollow it out", ["hollow"]),
        ("put it on a stand with a magnet", ["plinth", "magnet"]),
        ("make the inside hollow and give the flap a hinge", []),  # that is a design
        ("bigger eyes", []),
    ])
    def test_a_change_that_is_only_features(self, change, names):
        assert addons.from_change(change).names() == names


class TestFeatures:
    def test_a_plinth_widens_the_base_and_lifts_the_figure(self, tmp_path):
        stl = figure(tmp_path / "f.stl")
        before = trimesh.load(stl)
        addons.apply(stl, addons.parse("plinth"))
        after = trimesh.load(stl)
        assert after.extents[0] == pytest.approx(before.extents[0] + 8, abs=1.0)  # 4 mm margin each side
        assert after.extents[2] == pytest.approx(before.extents[2] + 2.6, abs=0.1)
        assert addons.solids(after) == 1 and after.bounds[0][2] == pytest.approx(0, abs=1e-6)

    def test_a_magnet_pocket_is_cut_in_the_underside(self, tmp_path):
        stl = figure(tmp_path / "f.stl", extents=(40, 36, 40))
        before = trimesh.load(stl).volume
        addons.apply(stl, addons.parse("magnet 10x3"))
        after = trimesh.load(stl)
        pocket = np.pi * 5.2 ** 2 * 3.2
        assert before - after.volume == pytest.approx(pocket, rel=0.1)
        assert after.contains([[0, 0, 1.5]]).tolist() == [False]  # the pocket is empty

    def test_a_base_too_small_for_the_magnet_is_refused(self, tmp_path):
        stl = figure(tmp_path / "f.stl", extents=(12, 12, 40))
        with pytest.raises(addons.AddonError, match="Add a plinth"):
            addons.apply(stl, addons.parse("magnet 10x3"))

    def test_with_a_plinth_the_magnet_goes_in_that(self, tmp_path):
        stl = figure(tmp_path / "f.stl", extents=(12, 12, 40))
        done = addons.apply(stl, addons.parse("plinth, magnet 10x3"))
        assert done["added"] == ["a plinth", "a magnet pocket"]

    def test_a_key_ring_loop_stands_on_top(self, tmp_path):
        stl = figure(tmp_path / "f.stl")
        before = trimesh.load(stl)
        addons.apply(stl, addons.parse("keyring"))
        after = trimesh.load(stl)
        assert after.extents[2] > before.extents[2] + 3
        assert addons.solids(after) == 1

    def test_hollowing_leaves_an_even_shell(self, tmp_path):
        stl = figure(tmp_path / "f.stl", extents=(40, 36, 50))
        before = trimesh.load(stl).volume
        addons.apply(stl, addons.parse("hollow"))
        after = trimesh.load(stl)
        assert after.volume < 0.5 * before and addons.solids(after) == 1
        assert after.contains([after.centroid]).tolist() == [False]  # empty in the middle

    def test_open_top_makes_a_pot(self, tmp_path):
        stl = figure(tmp_path / "f.stl", extents=(50, 44, 30))
        addons.apply(stl, addons.parse("hollow open top"))
        after = trimesh.load(stl)
        # A ray dropped into the middle from above goes in without hitting a roof.
        top = after.bounds[1][2] + 5
        hits, _, _ = after.ray.intersects_location([[0, 0, top]], [[0, 0, -1]], multiple_hits=False)
        assert len(hits) == 1 and hits[0][2] < 5  # it lands on the floor


class TestCommands:
    def test_a_keychain_sculpture_gets_its_loop(self, bench, tmp_path):
        result = bench("printer.sculpt", title="Dragon", prompt="a dragon keychain", height_mm=40)
        assert result.ok, result.speech
        part = record(bench, result.result["name"])
        assert part["addons"] == {"keyring": {"hole": 5.0, "band": 2.5, "t": 3.0}}
        assert part["added"] == ["a key ring loop"] and part["size_mm"][2] > 40

    def test_adding_a_feature_is_a_new_version_from_the_same_mesh(self, bench):
        first = bench("printer.sculpt", title="Owl", prompt="an owl", height_mm=40).result["name"]
        result = bench("printer.adjust", name=first, change="add a key ring loop")
        assert result.ok, result.speech
        new = record(bench, result.result["name"])
        assert new["mode"] == "addons" and new["parent"] == first and new["version"] == 2
        assert new["added"] == ["a key ring loop"] and bench.edits == []  # no picture edit

    def test_features_accumulate_across_versions(self, bench):
        first = bench("printer.sculpt", title="Owl", prompt="an owl", height_mm=40).result["name"]
        second = bench("printer.adjust", name=first, addons="plinth").result["name"]
        third = bench("printer.adjust", name=second, change="add a key ring loop")
        assert third.ok, third.speech
        new = record(bench, third.result["name"])
        assert set(new["addons"]) == {"plinth", "keyring"} and new["added"] == ["a key ring loop"]
