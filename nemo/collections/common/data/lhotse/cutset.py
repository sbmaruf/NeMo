import logging
import warnings
from pathlib import Path
from typing import Any, NewType, Sequence, Tuple

from nemo.collections.common.data.lhotse.nemo_adapters import LazyNeMoIterator, LazyNeMoTarredIterator

LhotseCutSet = NewType("LhotseCutSet", Any)  # Indicate return type without importing Lhotse.


def read_cutset_from_config(config) -> Tuple[LhotseCutSet, bool]:
    """
    Reads NeMo configuration and creates a CutSet either from Lhotse or NeMo manifests.

    Returns a tuple of ``CutSet`` and a boolean indicating whether the data is tarred (True) or not (False).
    """
    # First, we'll figure out if we should read Lhotse manifest or NeMo manifest.
    use_nemo_manifest = all(config.lhotse.get(opt) is None for opt in ("cuts_path", "shar_path"))
    if use_nemo_manifest:
        assert (
            config.get("manifest_filepath") is not None
        ), "You must specify either: manifest_filepath, lhotse.cuts_path, or lhotse.shar_path"
        is_tarred = config.get("tarred_audio_filepaths") is not None
    else:
        is_tarred = config.lhotse.get("shar_path") is not None
    if use_nemo_manifest:
        # Read NeMo manifest -- use the right wrapper depending on tarred/non-tarred.
        cuts = read_nemo_manifest(config, is_tarred)
    else:
        # Read Lhotse manifest (again handle both tarred(shar)/non-tarred).
        cuts = read_lhotse_manifest(config, is_tarred)
    return cuts, is_tarred


def read_lhotse_manifest(config, is_tarred: bool) -> LhotseCutSet:
    from lhotse import CutSet

    if is_tarred:
        # Lhotse Shar is the equivalent of NeMo's native "tarred" dataset.
        # The combination of shuffle_shards, and repeat causes this to
        # be an infinite manifest that is internally reshuffled on each epoch.
        # The parameter ``config.lhotse.shar_seed`` is used to determine shard shuffling order. Options:
        # - "trng" means we'll defer setting the seed until the iteration
        #   is triggered, and we'll use system TRNG to get a completely random seed for each worker.
        #   This results in every dataloading worker using full data but in a completely different order.
        # - "randomized" means we'll defer setting the seed until the iteration
        #   is triggered, and we'll use config.lhotse.seed to get a pseudo-random seed for each worker.
        #   This results in every dataloading worker using full data but in a completely different order.
        #   Unlike "trng", this is deterministic, and if you resume training, you should change the seed
        #   to observe different data examples than in the previous run.
        # - integer means we'll set a specific seed in every worker, and data would be duplicated across them.
        #   This is mostly useful for unit testing or debugging.
        shar_seed = config.lhotse.get("shar_seed", "trng")
        if config.lhotse.get("cuts_path") is not None:
            warnings.warn("Note: lhotse.cuts_path will be ignored because lhotse.shar_path was provided.")
        if isinstance(config.lhotse.shar_path, (str, Path)):
            logging.info(
                f"Initializing Lhotse Shar CutSet (tarred) from a single data source: '{config.lhotse.shar_path}'"
            )
            cuts = CutSet.from_shar(in_dir=config.lhotse.shar_path, shuffle_shards=True, seed=shar_seed).repeat()
        else:
            # Multiple datasets in Lhotse Shar format: we will dynamically multiplex them
            # with probability approximately proportional to their size
            logging.info(
                "Initializing Lhotse Shar CutSet (tarred) from multiple data sources with a weighted multiplexer. "
                "We found the following sources and weights: "
            )
            cutsets = []
            weights = []
            for item in config.lhotse.shar_path:
                if isinstance(item, (str, Path)):
                    path = item
                    cs = CutSet.from_shar(in_dir=path, shuffle_shards=True, seed=shar_seed)
                    weight = len(cs)
                else:
                    assert isinstance(item, Sequence) and len(item) == 2 and isinstance(item[1], (int, float)), (
                        "Supported inputs types for config.lhotse.shar_path are: "
                        "str | list[str] | list[tuple[str, number]] "
                        "where str is a path and number is a mixing weight (it may exceed 1.0). "
                        f"We got: '{item}'"
                    )
                    path, weight = item
                    cs = CutSet.from_shar(in_dir=path, shuffle_shards=True, seed=shar_seed)
                logging.info(f"- {path=} {weight=}")
                cutsets.append(cs.repeat())
                weights.append(weight)
            cuts = CutSet.mux(*cutsets, weights=weights)
    else:
        # Regular Lhotse manifest points to individual audio files (like native NeMo manifest).
        cuts = CutSet.from_file(config.lhotse.cuts_path)
    return cuts


def read_nemo_manifest(config, is_tarred: bool) -> LhotseCutSet:
    from lhotse import CutSet

    common_kwargs = {
        "text_field": config.lhotse.get("text_field", "text"),
        "lang_field": config.lhotse.get("lang_field", "lang"),
    }
    print(common_kwargs)
    shuffle = config.get("shuffle", False)

    if is_tarred:
        if isinstance(config["manifest_filepath"], (str, Path)):
            logging.info(
                f"Initializing Lhotse CutSet from a single NeMo manifest (tarred): '{config['manifest_filepath']}'"
            )
            cuts = CutSet(
                LazyNeMoTarredIterator(
                    config["manifest_filepath"],
                    tar_paths=config["tarred_audio_filepaths"],
                    shuffle_shards=shuffle,
                    **common_kwargs,
                )
            )
        else:
            # Assume it's [[path1], [path2], ...] (same for tarred_audio_filepaths).
            # This is the format for multiple NeMo buckets.
            # Note: we set "weights" here to be proportional to the number of utterances in each data source.
            #       this ensures that we distribute the data from each source uniformly throughout each epoch.
            #       Setting equal weights would exhaust the shorter data sources closer towatds the beginning
            #       of an epoch (or over-sample it in the case of infinite CutSet iteration with .repeat()).
            logging.info(
                f"Initializing Lhotse CutSet from multiple tarred NeMo manifest sources with a weighted multiplexer. "
                f"We found the following sources and weights: "
            )
            cutsets = []
            weights = []
            for (mp,), (tp,) in zip(config["manifest_filepath"], config["tarred_audio_filepaths"]):
                cutsets.append(
                    CutSet(
                        LazyNeMoTarredIterator(manifest_path=mp, tar_paths=tp, shuffle_shards=shuffle, **common_kwargs)
                    )
                )
                weights.append(len(cutsets[-1]))
                logging.info(f"- path={mp} weight={weights[-1]}")
            cuts = CutSet.mux(*cutsets, weights=weights)
    else:
        logging.info(
            f"Initializing Lhotse CutSet from a single NeMo manifest (non-tarred): '{config['manifest_filepath']}'"
        )
        cuts = CutSet(LazyNeMoIterator(config["manifest_filepath"], **common_kwargs))
    return cuts