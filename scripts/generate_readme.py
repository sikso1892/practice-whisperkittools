#
# For licensing see accompanying LICENSE.md file.
# Copyright (C) 2024 Argmax, Inc. All Rights Reserved.
#
import argparse
import evaluate
import json
import os
from collections import defaultdict

import pandas as pd
from argmaxtools.utils import get_logger
from huggingface_hub import HfApi, snapshot_download

from whisperkit._constants import EVALS_REPO_ID, MODEL_REPO_ID

wer_metric = evaluate.load("wer")

logger = get_logger(__name__)

QOI_KEY = "QoI (↑)"
FILE_SIZE_KEY = "File Size (MB)"
WER_KEY = "WER (↓)"
COMMIT_KEY = "Code Commit"

HF_HUB_DATASET_CARD_YAML_PREFIX = """
---
pretty_name: "WhisperKit ASR Evaluation Results"
viewer: false
library_name: whisperkit
tags:
- whisper
- whisperkit
- coreml
- asr
- quantized
---
# WhisperKit Transcription Quality\n
"""

HF_HUB_METRIC_EXPLANATION = """
### Explanation

We believe that rigorously measuring the quality of inference is necessary for developers and
enterprises to make informed decisions when opting to use optimized or compressed variants of
any machine learning model in production. To contextualize `WhisperKit`, we take the following Whisper
implementations and benchmark them using a consistent evaluation harness:

Server-side:
- `WhisperOpenAIAPI`: [OpenAI's Whisper API](https://platform.openai.com/docs/guides/speech-to-text)
\n($0.36 per hour of audio as of 02/29/24, 25MB file size limit per request)

On-device:
- `WhisperKit`: Argmax's implementation [[Eval Harness]](https://github.com/argmaxinc/whisperkittools/blob/main/whisperkit/pipelines.py#L100) [[Repo]](https://github.com/argmaxinc/WhisperKit)
- `whisper.cpp`: A C++ implementation form ggerganov [[Eval Harness]](https://github.com/argmaxinc/whisperkittools/blob/main/whisperkit/pipelines.py#L212) [[Repo]](https://github.com/ggerganov/whisper.cpp)
- `WhisperMLX`: A Python implementation from Apple MLX [[Eval Harness]](https://github.com/argmaxinc/whisperkittools/blob/main/whisperkit/pipelines.py#L338) [[Repo]](https://github.com/ml-explore/mlx-examples/blob/main/whisper/whisper/transcribe.py)
\n(All on-device implementations are available for free under MIT license as of 03/19/2024)

`WhisperOpenAIAPI` sets the reference and we assume that it is using the equivalent of [openai/whisper-large-v2](https://huggingface.co/openai/whisper-large-v2)
in float16 precision along with additional undisclosed optimizations from OpenAI. In all measurements, we care primarily about per-example no-regressions (quantified as `qoi` below)
which is a stricter metric compared to dataset average [Word Error RATE (WER)](https://en.wikipedia.org/wiki/Word_error_rate). A 100% `qoi` preserves perfect backwards-compatibility on the test distribution and avoids "perceived regressions", the phenomenon
where per-example known behavior changes after a code/model update and causes divergence in downstream code or breaks the user experience itself (even if dataset averages might stay flat
across updates). Pseudocode for `qoi`:

```python
qoi = []
for example in dataset:
    no_regression = wer(optimized_model(example)) <= wer(reference_model(example))
    qoi.append(no_regression)
qoi = (sum(qoi) / len(qoi)) * 100.
```

Note that the ordering of models with respect to `WER` does not necessarily match the ordering with respect to `QoI`. This is because the reference model gets assigned
a QoI of 100% by definition. Any per-example regression by other implementations get penalized while per-example improvements are not rewarded. `QoI` (higher is better) matters
where the production behavior is established by the reference results and the goal is to not regress when switching to an optimized or compressed model. On the other hand,
`WER` (lower is better) matters when there is no established production behavior and one is picking the best quality versus model size trade off point.

We anticipate developers that use Whisper (or similar models) in production to have their own Quality Assurance test sets and [whisperkittools](https://github.com/argmaxinc/whisperkittools) offers
the tooling necessary to run the same measurements on such custom test sets, please see the [Model Evaluation on Custom Dataset]((https://github.com/argmaxinc/whisperkittools)) for details.

### Why are there so many Whisper versions?
WhisperKit is an SDK for building speech-to-text features in apps across a wide range of Apple devices. We are working towards abstracting away the model versioning from the developer so WhisperKit
"just works" by deploying the highest-quality model version that a particular device can execute. In the interim, we leave the choice to the developer by providing quality and size trade-offs.


### Datasets
- [librispeech](https://huggingface.co/datasets/argmaxinc/librispeech): ~5 hours of short English audio clips, tests short-form transcription quality
- [earnings22](https://huggingface.co/datasets/argmaxinc/earnings22): ~120 hours of English audio clips from earnings calls with various accents, tests long-form transcription quality

### Reproducing Results
Benchmark results on this page were automatically generated by [whisperkittools](https://github.com/argmaxinc/whisperkittools) using our cluster of Apple Silicon Macs as self-hosted runners on
Github Actions. We periodically recompute these benchmarks as part of our CI pipeline. Due to [security concerns](https://docs.github.com/en/actions/security-guides/security-hardening-for-github-actions#hardening-for-self-hosted-runners),
we are unable to open up the cluster to the public. However, any Apple Silicon Mac (even with 8GB RAM) can be used to
run identical [evaluation jobs](#evaluation) locally. For reference, our M2 Ultra devices complete a `librispeech` + `openai/whisper-large-v3`
evaluation in under 1 hour regardless of the Whisper implementation. Oldest Apple Silicon Macs should take less than 1 day to complete the same evaluation.

"""  # noqa: E501

HF_HUB_GLOSSARY = """
### Glossary

- `_turbo`: Indicates the presence of additional optimizations (not compression) to unlock streaming transcription
as described in our [Blog Post](https://www.takeargmax.com/blog/whisperkit).

- `_*MB`: Indicates the presence of model compression. Instead of cluttering the filename with details like
`_AudioEncoder-5.8bits_TextDecoder-6.1bits_QLoRA-rank=16`, we choose to summarize the compression spec as the
resulting total file size since this is what matters to developers in production.

"""  # noqa: E501

# TODO(atiorh): Read remote git file size
REFERENCE_MODEL_FILE_SIZES = {
    "WhisperKit/openai_whisper-large-v2": 3100,                 # MB
    "WhisperKit/openai_whisper-large-v2_turbo": 3100,           # MB
    "WhisperKit/openai_whisper-large-v3": 3100,                 # MB
    "WhisperKit/openai_whisper-large-v3_turbo": 3100,           # MB
    "WhisperKit/openai_whisper-small": 483,                     # MB
    "WhisperKit/openai_whisper-small.en": 483,                  # MB
    "WhisperKit/openai_whisper-base": 145,                      # MB
    "WhisperKit/openai_whisper-base.en": 145,                   # MB
    "WhisperKit/openai_whisper-tiny": 66,                       # MB
    "WhisperKit/openai_whisper-tiny.en": 66,                    # MB
    "whisper.cpp/openai_whisper-large-v2-q5_0": 1080,           # MB
    "whisper.cpp/openai_whisper-large-v3-q5_0": 1080,           # MB
    "whisper.cpp/openai_whisper-large-v3": 3100,                # MB
    "whisper.cpp/openai_whisper-large-v2": 3100,                # MB
    "WhisperOpenAIAPI/openai_whisper-large-v2": 3100,           # MB
    "WhisperKit/distil-whisper_distil-large-v3": 1510,          # MB
    "WhisperKit/distil-whisper_distil-large-v3_turbo": 1510,    # MB
}

DATASET_CAPTIONS = {
    "librispeech": "Short-form Audio (<30s/clip) - 5 hours of English audiobook clips",
    "earnings22": "Long-Form Audio (>1hr/clip) - 120 hours of earnings call recordings in English with various accents",
}

REPO_URLS = {
    "whisper.cpp": "https://github.com/ggerganov/whisper.cpp",
    "WhisperKit": "https://github.com/argmaxinc/WhisperKit"
}


def cli():
    f""" Generates the README for hf.co/datasets/{EVALS_REPO_ID} which contains
    Quality-of-Inference (QoI) certifications for Whisper models.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reference-to-optimized-mapping",
        action="append",
        help="Mapping of reference model version to optimized model version. "
             "Syntax: --reference-to-optimized-mapping <reference>:<optimized1,...,optimizedN>. "
             "Specify multiple times to add more mappings."
    )
    parser.add_argument(
        "--upload-results",
        action="store_true",
        help=f"If specified, uploads the generated README to hf.co/datasets/{EVALS_REPO_ID}"
    )
    parser.add_argument(
        "--dataset-names",
        action="append",
        default=[],
        help="Dataset names to generate README for. Specify multiple times to add more datasets."
    )
    args = parser.parse_args()

    readme = ""

    for dataset_name in args.dataset_names:
        readme += f"\n## Dataset: `{dataset_name}`\n{DATASET_CAPTIONS[dataset_name]}\n"
        "-------------------------------------------------"

        # Quality-of-Inference (QoI) certifications for Whisper models
        for mapping in args.reference_to_optimized_mapping:
            results_dict = {}
            reference, optimized_csv = mapping.split(":")
            results_dict[WER_KEY] = defaultdict(float)
            results_dict[QOI_KEY] = defaultdict(float)
            results_dict[FILE_SIZE_KEY] = defaultdict(int)
            results_dict[COMMIT_KEY] = defaultdict(str)

            # Fetch the reference eval results
            reference_code_repo, reference_model = parse_name(reference)

            reference_eval, reference_link = get_latest_eval(
                reference_code_repo, dataset_name, reference_model)

            reference_key = reference.rsplit('/')[
                -1].replace('openai_whisper-', '').replace('distil-whisper_', '')
            if reference_code_repo == "WhisperKit":
                reference_key = \
                    f"[{reference_key}]" \
                    f"({get_model_link(reference_model)}) "
            else:
                reference_key = reference_key + f" ({reference_code_repo})"

            # Fill reference model version values
            results_dict[QOI_KEY][reference_key] = 100.  # By definition of QoI
            results_dict[FILE_SIZE_KEY][reference_key] = \
                REFERENCE_MODEL_FILE_SIZES[reference]

            # Sample average WER for reference model
            results_dict[WER_KEY][reference_key] = \
                f"[{compute_average_wer(reference_eval['results'])}]({reference_link})"

            # Add commit hash for reference results
            commit_hash = reference_eval["metadata"]["inference_context"]["code_spec"]["code_commit_hash"]
            if commit_hash is not None:
                results_dict[COMMIT_KEY][reference_key] = \
                    f"[Link]({REPO_URLS[reference_code_repo]}/commit/{commit_hash[:7]})"
            else:
                results_dict[COMMIT_KEY][reference_key] = "N/A"

            # Fill optimized model version values
            for optimized in optimized_csv.split(","):
                optimized_code_repo, optimized_model = parse_name(optimized)
                try:
                    optimized_eval, optimized_link = get_latest_eval(
                        optimized_code_repo, dataset_name, optimized_model)
                except Exception as e:
                    logger.warning(f"Could not fetch eval JSON for {optimized}: {e}")
                    continue

                optimized_key = optimized.rsplit('/')[
                    -1].replace('openai_whisper-', '').replace('distil-whisper_', '')
                if optimized_code_repo == "WhisperKit":
                    optimized_key = \
                        f"[{optimized_key}]" \
                        f"({get_model_link(optimized_model)}) "
                else:
                    optimized_key = optimized_key + f" ({optimized_code_repo})"

                # Verify fetched evals are comparable
                logger.info(f"Compare {optimized_link} vs {reference_link}")
                verify_apples_to_apples(reference_eval, optimized_eval)
                qoi = compute_quality_of_inference(
                    reference_eval["results"],
                    optimized_eval["results"]
                )
                results_dict[QOI_KEY][optimized_key] = qoi["no_regression"]
                results_dict[WER_KEY][optimized_key] = \
                    f"[{compute_average_wer(optimized_eval['results'])}]({optimized_link})"

                # Add commit hash for reference results
                commit_hash = optimized_eval["metadata"]["inference_context"]["code_spec"]["code_commit_hash"]
                if commit_hash is not None:
                    results_dict[COMMIT_KEY][optimized_key] = \
                        f"[Link]({REPO_URLS[optimized_code_repo]}/commit/{commit_hash[:7]})"
                else:
                    results_dict[COMMIT_KEY][optimized_key] = "N/A"

                # TODO(atiorh): Read remote git file size
                if optimized in REFERENCE_MODEL_FILE_SIZES:
                    file_size = REFERENCE_MODEL_FILE_SIZES[optimized]
                else:
                    suffix = optimized.rsplit("_")[-1]
                    if "MB" in suffix:
                        file_size = int(suffix.replace("MB", ""))
                    else:
                        file_size = "N/A"

                results_dict[FILE_SIZE_KEY][optimized_key] = file_size

            # Generate the README
            markdown_table = pd.DataFrame.from_dict(results_dict).to_markdown()
            readme += f"\n{markdown_table}\n"

    logger.info("Generated README:\n" + readme)

    if args.upload_results:
        temp_path = "/tmp/README.md"
        hub_readme = f"{HF_HUB_DATASET_CARD_YAML_PREFIX}\n{readme}\n{HF_HUB_METRIC_EXPLANATION}\n" + \
            f"{HF_HUB_GLOSSARY}"
        with open(temp_path, "w") as f:
            f.write(hub_readme)

        # Upload to f'hf.co/datasets/{EVALS_REPO_ID}'
        api = HfApi()
        api.upload_file(
            path_in_repo="README.md",
            path_or_fileobj=temp_path,
            repo_id=EVALS_REPO_ID,
            repo_type="dataset",
            commit_message="whisperkittools generated README.md",
        )
        api.upload_file(
            path_in_repo="README.md",
            path_or_fileobj=temp_path,
            repo_id=MODEL_REPO_ID,
            repo_type="model",
            commit_message="whisperkittools generated README.md",
        )
        logger.info("Uploaded to HF Hub")


def compute_quality_of_inference(reference, optimized, metric="wer"):
    """ Computes the Quality-of-Inference (QoI) certification for a given metric

    The certification comprises:
    - no_regression: the percentage of samples that didn't regress
    - improved: the percentage of samples that improved (indicentally)
    """
    no_regression = 0
    improved = 0
    count = 0

    for ref, opt in zip(reference, optimized):
        if opt[metric] <= ref[metric]:
            no_regression += 1
        if opt[metric] < ref[metric]:
            improved += 1
        count += 1

    return dict(
        no_regression=round(no_regression / count, 3) * 100.,
        improved=round(improved / count, 3) * 100.,
    )


def parse_name(result, default_code_repo="WhisperKit"):
    tokens = result.rsplit("/")
    if len(tokens) == 1:
        code_repo = default_code_repo
        model = result
    elif len(tokens) == 2:
        code_repo = tokens[0]
        model = tokens[1]
    else:
        raise ValueError(f"Invalid result name: {result}")

    return code_repo, model


def get_latest_eval(code_repo, dataset_name, model_version, local_dir="external"):
    f""" Fetch the latest eval from hf.co/datasets/{EVALS_REPO_ID}
    for given code repo, model version and dataset
    """
    os.makedirs(local_dir, exist_ok=True)
    repo_rel_dir = os.path.join(code_repo, model_version, dataset_name)
    _ = snapshot_download(
        repo_id=EVALS_REPO_ID,
        repo_type="dataset",
        local_dir=local_dir,
        allow_patterns=os.path.join(repo_rel_dir, "*.json")
    )

    # Filenames are chronological
    all_results = sorted(os.listdir(os.path.join(local_dir, repo_rel_dir)))
    if len(all_results) == 0:
        raise FileNotFoundError(f"No eval results found for {repo_rel_dir}")
    latest_result = os.path.join(local_dir, repo_rel_dir, all_results[-1])

    logger.info(f"Fetched {latest_result}")
    with open(latest_result, "r") as f:
        results = json.load(f)

    hub_link = f"https://hf.co/datasets/{EVALS_REPO_ID}/tree/main/{repo_rel_dir}"

    return results, hub_link


def verify_apples_to_apples(reference_eval, optimized_eval):
    """ Compare metadata from the inference context for any potential discrepancies
    """
    # Verify evals were generated with the same WhisperKit code commit
    hash1 = reference_eval["metadata"]["inference_context"]["code_spec"]["code_commit_hash"]
    hash2 = optimized_eval["metadata"]["inference_context"]["code_spec"]["code_commit_hash"]
    if hash1 != hash2:
        logger.warning("Reference and optimized evals weren't generated with the same code commit!")

    # Verify evals were generated with the same OS version
    osv1 = reference_eval["metadata"]["inference_context"]["os_spec"]["os_build_number"]
    osv2 = optimized_eval["metadata"]["inference_context"]["os_spec"]["os_build_number"]
    if osv1 != osv2:
        logger.warning("Reference and optimized evals weren't generated with the same OS version!")

    # Verify whisperkittools commit that orchestrated the tests
    wkt_commit_1 = reference_eval["metadata"]["whisperkittools_commit_hash"]
    wkt_commit_2 = optimized_eval["metadata"]["whisperkittools_commit_hash"]
    if wkt_commit_1 != wkt_commit_2:
        logger.warning(
            "Reference and optimized evals weren't generated with the same "
            "whisperkittools commit")


def compute_average_wer(results):
    return round(wer_metric.compute(
        references=[result["reference"] for result in results],
        predictions=[result["prediction"] for result in results],
    ) * 100., 2)


def get_model_link(model_version):
    return f"https://hf.co/{MODEL_REPO_ID}/tree/main/{model_version}"
