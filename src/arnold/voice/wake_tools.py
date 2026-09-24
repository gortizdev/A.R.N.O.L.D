"""Managing wake-word models: list, install, test, train.

A wake word is a small classifier on top of openWakeWord's shared embedding,
and openWakeWord ships four. Any other name means a model of your own, and
this is where those live: `models/wake/<name>.onnx`, referenced from config
by bare name.

Training drives openWakeWord's own `train.py` rather than re-implementing
it. Its two data stages (`--generate_clips`, `--augment_clips`) run as child
processes exactly as upstream intends; the final training stage is run
in-process, because upstream builds a DataLoader with worker processes over
a generator, and on Windows those workers have to pickle the generator and
cannot. Same maths, no workers, and no tflite export at the end - only the
.onnx is ever loaded here.

None of the training dependencies are imported at module level. Listing and
testing must work on a machine that will never train anything.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import process
from .audio import AudioError, WakeWordDetector, bundled_wake_words, resolve_wake_word

log = logging.getLogger(__name__)


class WakeToolError(RuntimeError):
    """Something the user can act on; printed as a sentence, never a traceback."""


_NAME_RE = re.compile(r"^[a-z0-9_]+$")

# What `openwakeword.train` and `openwakeword.data` import at the top.
TRAIN_DEPS = (
    "torch",
    "torchaudio",
    "torchinfo",
    "torchmetrics",
    "speechbrain",
    "audiomentations",
    "torch_audiomentations",
    "acoustics",
    "mutagen",
    "pronouncing",
    "onnx",
)

NOTEBOOK_URL = (
    "https://github.com/dscripka/openWakeWord/blob/main/notebooks/"
    "automatic_model_training.ipynb"
)
# Opens the same notebook straight in Colab, no drive id needed.
COLAB_URL = (
    "https://colab.research.google.com/github/dscripka/openWakeWord/blob/main/"
    "notebooks/automatic_model_training.ipynb"
)
TORCH_CUDA_INDEX = "https://download.pytorch.org/whl/cu128"


# -- names ---------------------------------------------------------------------


def phrase_to_name(phrase: str) -> str:
    """'Hey Athena!' -> 'hey_athena', the way openWakeWord names its own."""
    name = re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")
    if not name:
        raise WakeToolError(f"{phrase!r} does not make a model name")
    return name


def _check_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise WakeToolError(
            f"{name!r} is not a usable model name: letters, digits and underscores only"
        )
    return name


def wake_dir(config) -> Path:
    return Path(config.voice.wake_word_dir or "models/wake").expanduser()


# -- list / install ------------------------------------------------------------


def list_models(config) -> list[dict]:
    """Bundled and custom models, with the active one marked."""
    try:
        active = resolve_wake_word(config.voice.wake_word, wake_dir(config))
        active_key = (active.kind == "bundled", active.key, active.model_arg)
    except AudioError:
        active_key = None

    entries = []
    for name in bundled_wake_words():
        entries.append({
            "kind": "bundled",
            "name": name,
            "path": "",
            "active": active_key == (True, name, name),
        })
    folder = wake_dir(config)
    if folder.is_dir():
        for path in sorted(folder.glob("*.onnx")):
            resolved = str(path.resolve())
            entries.append({
                "kind": "custom",
                "name": path.stem,
                "path": str(path),
                "active": active_key is not None
                and not active_key[0]
                and active_key[2] == resolved,
            })
    # A model given by path, outside the folder, is still the active one.
    if active_key is not None and not active_key[0] and not any(e["active"] for e in entries):
        entries.append({
            "kind": "file",
            "name": active_key[1],
            "path": active_key[2],
            "active": True,
        })
    return entries


def install_model(config, src: str | Path, name: str | None = None, *, force: bool = False,
                  check: bool = True) -> Path:
    """Copy a model into the wake-word directory under `name`.

    It is loaded once first, so a file that is not an openWakeWord model is
    refused here rather than at the next wake word.
    """
    src = Path(src).expanduser()
    if src.suffix.lower() != ".onnx":
        raise WakeToolError(f"{src.name} is not an .onnx file; only ONNX models are loaded here")
    if not src.is_file():
        raise WakeToolError(f"{src} does not exist")
    name = _check_name(name or src.stem)

    if check:
        try:
            WakeWordDetector(str(src), 0.5)
        except (AudioError, ImportError) as exc:
            raise WakeToolError(f"{src.name} would not load as a wake word model: {exc}") from exc

    folder = wake_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{name}.onnx"
    if dest.exists() and not force:
        if dest.resolve() == src.resolve():
            return dest
        raise WakeToolError(f"{dest} already exists; pass --force to replace it")
    if dest.resolve() != src.resolve():
        shutil.copy2(src, dest)
    log.info("installed wake word model %s as %r", src, name)
    return dest


# -- live test -----------------------------------------------------------------


def test_live(config, name: str | None = None, seconds: float = 20.0, out=print) -> int:
    """Listen on the configured microphone and show the score. Returns the
    number of times the detector fired."""
    from .audio import Microphone, resolve_device

    voice = config.voice
    wake_word = name or voice.wake_word
    detector = WakeWordDetector(
        wake_word,
        voice.wake_threshold,
        patience=voice.wake_patience,
        vad_threshold=voice.wake_vad_threshold,
        wake_dir=wake_dir(config),
    )
    device = resolve_device(voice.input_device, input=True)
    out(
        f"listening for {wake_word!r} ({detector.model.kind}) at {detector.threshold:.2f}, "
        f"{detector.patience} frame(s) in a row, for {seconds:.0f}s - Ctrl-C to stop"
    )

    hits = 0
    deadline = time.monotonic() + seconds
    peak = 0.0
    try:
        with Microphone(device) as mic:
            for frame in mic.frames(timeout=0.5):
                if time.monotonic() > deadline:
                    break
                if frame is None:
                    continue
                fired = detector.triggered(frame)
                score = detector.last_score
                peak = max(peak, score)
                bar = "#" * int(round(min(1.0, score) * 30))
                sys.stdout.write(f"\r{bar:<30} {score:.2f}  ")
                sys.stdout.flush()
                if fired:
                    hits += 1
                    sys.stdout.write("\n")
                    out(f"TRIGGERED (score {score:.2f})")
    except KeyboardInterrupt:
        pass
    sys.stdout.write("\n")
    out(f"{hits} trigger(s); peak score {peak:.2f}")
    return hits


# -- training ------------------------------------------------------------------


def missing_training_deps() -> list[str]:
    return [dep for dep in TRAIN_DEPS if importlib.util.find_spec(dep) is None]


def dependency_help(missing: list[str], phrase: str) -> str:
    return (
        f"wake train needs the training extra (missing: {', '.join(missing)}).\n"
        f'  uv pip install -e ".[wake-train]"\n'
        f"  # for the GPU, install torch from the CUDA index first:\n"
        f"  uv pip install torch torchaudio --index-url {TORCH_CUDA_INDEX}\n"
        f"Or train in the browser with openWakeWord's notebook and install the result:\n"
        f"  {COLAB_URL}\n"
        f'  (target phrase: "{phrase}")\n'
        f"  arnold wake install {phrase_to_name(phrase)}.onnx"
    )


@dataclass(frozen=True)
class Asset:
    key: str
    relative: str
    what: str
    source: str
    is_dir: bool = False


TRAINING_ASSETS: tuple[Asset, ...] = (
    Asset(
        "piper_generator",
        "piper-sample-generator",
        "the synthetic speech generator (a checkout with generate_samples.py)",
        "git clone https://github.com/rhasspy/piper-sample-generator",
        is_dir=True,
    ),
    Asset(
        "piper_checkpoint",
        "piper-sample-generator/models/en_US-libritts_r-medium.pt",
        "the generator's LibriTTS-R checkpoint",
        "https://github.com/rhasspy/piper-sample-generator/releases/download/v2.0.0/"
        "en_US-libritts_r-medium.pt",
    ),
    Asset(
        "acav100m",
        "features/acav100m.npy",
        "~2000 hours of pre-computed negative features (large)",
        "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
        "openwakeword_features_ACAV100M_2000_hrs_16bit.npy",
    ),
    Asset(
        "validation",
        "features/validation.npy",
        "11 hours of validation features, for the false-positive rate",
        "https://huggingface.co/datasets/davidscripka/openwakeword_features/resolve/main/"
        "validation_set_features.npy",
    ),
    Asset(
        "rir",
        "rir",
        "a folder of room impulse responses (WAV)",
        "https://huggingface.co/datasets/davidscripka/MIT_environmental_impulse_responses",
        is_dir=True,
    ),
    Asset(
        "background",
        "background",
        "a folder of 16 kHz WAV background noise (the notebook uses HF agkphysics/AudioSet "
        "bal_train09 resampled to 16 kHz, and rudraml/fma)",
        NOTEBOOK_URL,
        is_dir=True,
    ),
)


class TrainingLayout:
    """Where the training assets live: `<wake_word_dir>/train` by default."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser()

    def path(self, key: str) -> Path:
        asset = next(a for a in TRAINING_ASSETS if a.key == key)
        return self.data_dir / asset.relative

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "out"

    def missing(self) -> list[Asset]:
        absent = []
        for asset in TRAINING_ASSETS:
            path = self.data_dir / asset.relative
            if asset.is_dir:
                ok = path.is_dir() and any(path.iterdir())
            else:
                ok = path.is_file()
            if not ok:
                absent.append(asset)
        return absent

    def describe_missing(self, absent: list[Asset], phrase: str) -> str:
        lines = [f"wake train needs these under {self.data_dir}:"]
        for asset in absent:
            lines.append(f"  {asset.relative:<58} {asset.what}")
            lines.append(f"  {'':<58} {asset.source}")
        lines.append("Or skip all of it and train in the browser:")
        lines.append(f"  {COLAB_URL}")
        lines.append(f'  (target phrase: "{phrase}")')
        lines.append(f"  arnold wake install {phrase_to_name(phrase)}.onnx")
        return "\n".join(lines)


def write_training_config(layout: TrainingLayout, phrase: str, name: str, *,
                          steps: int = 10000, samples: int = 1000) -> Path:
    """The YAML `openwakeword.train` reads. Every key it looks up is here."""
    import yaml

    layout.output_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "target_phrase": [phrase],
        "model_name": name,
        "output_dir": str(layout.output_dir),
        "n_samples": int(samples),
        "n_samples_val": max(100, int(samples) // 10),
        "tts_batch_size": 50,
        "custom_negative_phrases": [],
        "piper_sample_generator_path": str(layout.path("piper_generator")),
        "rir_paths": [str(layout.path("rir"))],
        "background_paths": [str(layout.path("background"))],
        "background_paths_duplication_rate": [1],
        "augmentation_rounds": 1,
        "augmentation_batch_size": 16,
        "feature_data_files": {"ACAV100M_sample": str(layout.path("acav100m"))},
        "false_positive_validation_data_path": str(layout.path("validation")),
        "batch_n_per_class": {
            "ACAV100M_sample": 1024,
            "adversarial_negative": 50,
            "positive": 50,
        },
        "model_type": "dnn",
        "layer_size": 32,
        "steps": int(steps),
        "max_negative_weight": 1500,
        "target_false_positives_per_hour": 0.2,
    }
    path = layout.output_dir / f"{name}.training.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


STAGES = ("generate", "augment", "train")


def train(config, phrase: str, name: str | None = None, *, steps: int = 10000,
          samples: int = 1000, data_dir: str | Path | None = None,
          stages: tuple[str, ...] = STAGES, out=print) -> Path:
    """Run the pipeline and return the trained .onnx (not yet installed)."""
    phrase = " ".join(phrase.split())
    if not phrase:
        raise WakeToolError("give me the phrase to train, e.g. \"hey athena\"")
    name = _check_name(name or phrase_to_name(phrase))
    for stage in stages:
        if stage not in STAGES:
            raise WakeToolError(f"unknown stage {stage!r}; stages are {', '.join(STAGES)}")

    layout = TrainingLayout(data_dir or (wake_dir(config) / "train"))
    training_config = write_training_config(layout, phrase, name, steps=steps, samples=samples)
    out(f"training config: {training_config}")

    base = [sys.executable, "-m", "openwakeword.train", "--training_config", str(training_config)]
    for stage, flag in (("generate", "--generate_clips"), ("augment", "--augment_clips")):
        if stage not in stages:
            continue
        out(f"-- {stage} --")
        completed = process.run(
            base + [flag], capture_output=False, cwd=str(layout.data_dir), check=False
        )
        if completed.returncode != 0:
            raise WakeToolError(
                f"the {stage} stage failed (exit {completed.returncode}); see the output above"
            )

    model_path = layout.output_dir / f"{name}.onnx"
    if "train" in stages:
        out("-- train --")
        model_path = _train_in_process(training_config, out)
    if not model_path.is_file():
        raise WakeToolError(f"training finished but {model_path} was not produced")
    return model_path


def _total_length(positive_test_dir: Path) -> int:
    """openWakeWord's rule: the median generated clip plus 750 ms, rounded to
    the nearest 1000 samples, never under 2 seconds."""
    import numpy as np
    import scipy.io.wavfile

    clips = sorted(positive_test_dir.glob("*.wav"))
    if not clips:
        raise WakeToolError(
            f"no generated clips in {positive_test_dir}; run the generate stage first"
        )
    rng = np.random.default_rng(0)
    lengths = []
    for _ in range(min(50, len(clips))):
        _, data = scipy.io.wavfile.read(str(clips[rng.integers(0, len(clips))]))
        lengths.append(len(data))
    total = int(round(float(np.median(lengths)) / 1000) * 1000) + 12000
    if total < 32000 or abs(total - 32000) <= 4000:
        total = 32000
    return total


def _train_in_process(training_config: Path, out=print) -> Path:
    """`openwakeword.train --train_model`, minus the DataLoader workers and
    the tflite export. Follows train.py line for line otherwise."""
    import numpy as np
    import torch
    import yaml
    from openwakeword.data import mmap_batch_generator
    from openwakeword.train import Model as TrainModel
    from openwakeword.utils import AudioFeatures

    cfg = yaml.safe_load(training_config.read_text(encoding="utf-8"))
    output_dir = Path(cfg["output_dir"]).resolve()
    feature_dir = output_dir / cfg["model_name"]

    # The augment stage ran in its own process and picked total_length from a
    # random sample of clips; near a rounding boundary this process could
    # pick differently and the feature arrays would not fit the model. So
    # take the shape from the features it actually wrote, and only fall
    # back to recomputing when they are not there.
    positive_train = feature_dir / "positive_features_train.npy"
    if positive_train.is_file():
        input_shape = tuple(np.load(positive_train, mmap_mode="r").shape[1:])
        out(f"model input shape {input_shape} (from {positive_train.name})")
    else:
        total_length = _total_length(feature_dir / "positive_test")
        input_shape = AudioFeatures(device="cpu").get_embedding_shape(total_length // 16000)
        out(f"model input shape {input_shape} (from clip length {total_length})")
    model = TrainModel(
        n_classes=1,
        input_shape=input_shape,
        model_type=cfg["model_type"],
        layer_dim=cfg["layer_size"],
        seconds_per_example=1280 * input_shape[0] / 16000,
    )

    def reshape(x, n=16):
        if n > x.shape[1] or n < x.shape[1]:
            x = np.vstack(x)
            return np.array([x[i:i + n, :] for i in range(0, x.shape[0] - n, n)])
        return x

    data_files = dict(cfg["feature_data_files"])
    data_transforms = {key: reshape for key in data_files}
    data_files["positive"] = str(feature_dir / "positive_features_train.npy")
    data_files["adversarial_negative"] = str(feature_dir / "negative_features_train.npy")
    label_transforms = {
        key: (lambda x: [1 for _ in x]) if key == "positive" else (lambda x: [0 for _ in x])
        for key in data_files
    }

    generator = mmap_batch_generator(
        data_files,
        n_per_class=cfg["batch_n_per_class"],
        data_transform_funcs=data_transforms,
        label_transform_funcs=label_transforms,
    )

    class IterDataset(torch.utils.data.IterableDataset):
        def __init__(self, gen):
            self.gen = gen

        def __iter__(self):
            return self.gen

    # num_workers=0 is the whole point of running this here.
    x_train = torch.utils.data.DataLoader(IterDataset(generator), batch_size=None, num_workers=0)

    fp = np.load(cfg["false_positive_validation_data_path"])
    fp = np.array([fp[i:i + input_shape[0]] for i in range(0, fp.shape[0] - input_shape[0], 1)])
    fp_labels = np.zeros(fp.shape[0]).astype(np.float32)
    x_val_fp = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(torch.from_numpy(fp), torch.from_numpy(fp_labels)),
        batch_size=len(fp_labels),
    )

    val_pos = np.load(feature_dir / "positive_features_test.npy")
    val_neg = np.load(feature_dir / "negative_features_test.npy")
    labels = np.hstack((np.ones(val_pos.shape[0]), np.zeros(val_neg.shape[0]))).astype(np.float32)
    x_val = torch.utils.data.DataLoader(
        torch.utils.data.TensorDataset(
            torch.from_numpy(np.vstack((val_pos, val_neg))), torch.from_numpy(labels)
        ),
        batch_size=len(labels),
    )

    out(f"training {cfg['model_name']} for {cfg['steps']} steps on {model.device}")
    best = model.auto_train(
        X_train=x_train,
        X_val=x_val,
        false_positive_val_data=x_val_fp,
        steps=cfg["steps"],
        max_negative_weight=cfg["max_negative_weight"],
        target_fp_per_hour=cfg["target_false_positives_per_hour"],
    )
    model.export_model(model=best, model_name=cfg["model_name"], output_dir=str(output_dir))
    model_path = output_dir / f"{cfg['model_name']}.onnx"
    _fold_external_data(model_path)
    return model_path


def _fold_external_data(model_path: Path) -> None:
    """Newer torch exporters park the weights in a `<name>.onnx.data` beside
    the model. One file is what gets installed and loaded, so fold them in."""
    import onnx

    sidecar = model_path.with_name(model_path.name + ".data")
    if not sidecar.is_file():
        return
    onnx.save(onnx.load(str(model_path)), str(model_path), save_as_external_data=False)
    sidecar.unlink()
