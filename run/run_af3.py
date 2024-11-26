"""AlphaFold 3 structure prediction script.
"""

from collections.abc import Callable, Iterable, Sequence
import csv
import dataclasses
import datetime
import functools
import multiprocessing
import os
import pathlib
import shutil
import string
import textwrap
import time
import typing
from typing import Protocol, Self, TypeVar, overload, Dict, Any

from absl import flags
from alphafold3.common import base_config
from alphafold3.common import folding_input
from alphafold3.constants import chemical_components
import alphafold3.cpp
from alphafold3.data import featurisation
from alphafold3.data import pipeline
from alphafold3.jax.attention import attention
from alphafold3.model import features
from alphafold3.model import params
from alphafold3.model import post_processing
from alphafold3.model.components import base_model
from alphafold3.model.components import utils
from alphafold3.model.diffusion import model as diffusion_model
import haiku as hk
import jax
from jax import numpy as jnp
import numpy as np

from af3_utils import (
    get_af3_args,
    load_fold_inputs_from_path,
    load_fold_inputs_from_dir
)


_HOME_DIR = pathlib.Path(os.environ.get('HOME'))
_DEFAULT_DB_DIR = _HOME_DIR / 'public_databases'


# Binary paths.
_JACKHMMER_BINARY_PATH = flags.DEFINE_string(
    'jackhmmer_binary_path',
    shutil.which('jackhmmer'),
    'Path to the Jackhmmer binary.',
)
_NHMMER_BINARY_PATH = flags.DEFINE_string(
    'nhmmer_binary_path',
    shutil.which('nhmmer'),
    'Path to the Nhmmer binary.',
)
_HMMALIGN_BINARY_PATH = flags.DEFINE_string(
    'hmmalign_binary_path',
    shutil.which('hmmalign'),
    'Path to the Hmmalign binary.',
)
_HMMSEARCH_BINARY_PATH = flags.DEFINE_string(
    'hmmsearch_binary_path',
    shutil.which('hmmsearch'),
    'Path to the Hmmsearch binary.',
)
_HMMBUILD_BINARY_PATH = flags.DEFINE_string(
    'hmmbuild_binary_path',
    shutil.which('hmmbuild'),
    'Path to the Hmmbuild binary.',
)

# Database paths.
DB_DIR = flags.DEFINE_multi_string(
    'db_dir',
    (_DEFAULT_DB_DIR.as_posix(),),
    'Path to the directory containing the databases. Can be specified multiple'
    ' times to search multiple directories in order.',
)
_SMALL_BFD_DATABASE_PATH = flags.DEFINE_string(
    'small_bfd_database_path',
    '${DB_DIR}/bfd-first_non_consensus_sequences.fasta',
    'Small BFD database path, used for protein MSA search.',
)
_MGNIFY_DATABASE_PATH = flags.DEFINE_string(
    'mgnify_database_path',
    '${DB_DIR}/mgy_clusters_2022_05.fa',
    'Mgnify database path, used for protein MSA search.',
)
_UNIPROT_CLUSTER_ANNOT_DATABASE_PATH = flags.DEFINE_string(
    'uniprot_cluster_annot_database_path',
    '${DB_DIR}/uniprot_all_2021_04.fa',
    'UniProt database path, used for protein paired MSA search.',
)
_UNIREF90_DATABASE_PATH = flags.DEFINE_string(
    'uniref90_database_path',
    '${DB_DIR}/uniref90_2022_05.fa',
    'UniRef90 database path, used for MSA search. The MSA obtained by '
    'searching it is used to construct the profile for template search.',
)
_NTRNA_DATABASE_PATH = flags.DEFINE_string(
    'ntrna_database_path',
    '${DB_DIR}/nt_rna_2023_02_23_clust_seq_id_90_cov_80_rep_seq.fasta',
    'NT-RNA database path, used for RNA MSA search.',
)
_RFAM_DATABASE_PATH = flags.DEFINE_string(
    'rfam_database_path',
    '${DB_DIR}/rfam_14_9_clust_seq_id_90_cov_80_rep_seq.fasta',
    'Rfam database path, used for RNA MSA search.',
)
_RNA_CENTRAL_DATABASE_PATH = flags.DEFINE_string(
    'rna_central_database_path',
    '${DB_DIR}/rnacentral_active_seq_id_90_cov_80_linclust.fasta',
    'RNAcentral database path, used for RNA MSA search.',
)
_PDB_DATABASE_PATH = flags.DEFINE_string(
    'pdb_database_path',
    '${DB_DIR}/mmcif_files',
    'PDB database directory with mmCIF files path, used for template search.',
)
_SEQRES_DATABASE_PATH = flags.DEFINE_string(
    'seqres_database_path',
    '${DB_DIR}/pdb_seqres_2022_09_28.fasta',
    'PDB sequence database path, used for template search.',
)

# Number of CPUs to use for MSA tools.
_JACKHMMER_N_CPU = flags.DEFINE_integer(
    'jackhmmer_n_cpu',
    min(multiprocessing.cpu_count(), 8),
    'Number of CPUs to use for Jackhmmer. Default to min(cpu_count, 8). Going'
    ' beyond 8 CPUs provides very little additional speedup.',
)
_NHMMER_N_CPU = flags.DEFINE_integer(
    'nhmmer_n_cpu',
    min(multiprocessing.cpu_count(), 8),
    'Number of CPUs to use for Nhmmer. Default to min(cpu_count, 8). Going'
    ' beyond 8 CPUs provides very little additional speedup.',
)


class ConfigurableModel(Protocol):
    """A model with a nested config class."""

    class Config(base_config.BaseConfig):
        ...

    def __call__(self, config: Config) -> Self:
        ...

    @classmethod
    def get_inference_result(
        cls: Self,
        batch: features.BatchDict,
        result: base_model.ModelResult,
        target_name: str = '',
    ) -> Iterable[base_model.InferenceResult]:
        ...


ModelT = TypeVar('ModelT', bound=ConfigurableModel)


def make_model_config(
    *,
    model_class: type[ModelT] = diffusion_model.Diffuser,
    flash_attention_implementation: attention.Implementation = 'triton',
    num_diffusion_samples: int = 5,
):
    """Returns a model config with some defaults overridden."""
    config = model_class.Config()
    if hasattr(config, 'global_config'):
        config.global_config.flash_attention_implementation = (
            flash_attention_implementation
        )
    if hasattr(config, 'heads'):
        config.heads.diffusion.eval.num_samples = num_diffusion_samples
    return config


class ModelRunner:
    """Helper class to run structure prediction stages."""

    def __init__(
        self,
        model_class: ConfigurableModel,
        config: base_config.BaseConfig,
        device: jax.Device,
        model_dir: pathlib.Path,
    ):
        self._model_class = model_class
        self._model_config = config
        self._device = device
        self._model_dir = model_dir

    @functools.cached_property
    def model_params(self) -> hk.Params:
        """Loads model parameters from the model directory."""
        return params.get_model_haiku_params(model_dir=self._model_dir)

    @functools.cached_property
    def _model(
        self,
    ) -> Callable[[jnp.ndarray, features.BatchDict], base_model.ModelResult]:
        """Loads model parameters and returns a jitted model forward pass."""
        assert isinstance(self._model_config, self._model_class.Config)

        @hk.transform
        def forward_fn(batch):
            result = self._model_class(self._model_config)(batch)
            result['__identifier__'] = self.model_params['__meta__']['__identifier__']
            return result

        return functools.partial(
            jax.jit(forward_fn.apply, device=self._device), self.model_params
        )

    def run_inference(
        self, featurised_example: features.BatchDict, rng_key: jnp.ndarray
    ) -> base_model.ModelResult:
        """Computes a forward pass of the model on a featurised example."""
        featurised_example = jax.device_put(
            jax.tree_util.tree_map(
                jnp.asarray, utils.remove_invalidly_typed_feats(featurised_example)
            ),
            self._device,
        )

        result = self._model(rng_key, featurised_example)
        result = jax.tree.map(np.asarray, result)
        result = jax.tree.map(
            lambda x: x.astype(jnp.float32) if x.dtype == jnp.bfloat16 else x,
            result,
        )
        result['__identifier__'] = result['__identifier__'].tobytes()
        return result

    def extract_structures(
        self,
        batch: features.BatchDict,
        result: base_model.ModelResult,
        target_name: str,
    ) -> list[base_model.InferenceResult]:
        """Generates structures from model outputs."""
        return list(
            self._model_class.get_inference_result(
                batch=batch, result=result, target_name=target_name
            )
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class ResultsForSeed:
    """Stores the inference results (diffusion samples) for a single seed.

    Attributes:
        seed: The seed used to generate the samples.
        inference_results: The inference results, one per sample.
        full_fold_input: The fold input that must also include the results of
        running the data pipeline - MSA and templates.
    """

    seed: int
    inference_results: Sequence[base_model.InferenceResult]
    full_fold_input: folding_input.Input


def predict_structure(
    fold_input: folding_input.Input,
    model_runner: ModelRunner,
    buckets: Sequence[int] | None = None,
) -> Sequence[ResultsForSeed]:
    """Runs the full inference pipeline to predict structures for each seed."""

    print(f'Featurising data for seeds {fold_input.rng_seeds}...')
    featurisation_start_time = time.time()
    ccd = chemical_components.cached_ccd(user_ccd=fold_input.user_ccd)
    featurised_examples = featurisation.featurise_input(
        fold_input=fold_input, buckets=buckets, ccd=ccd, verbose=True
    )
    print(
        f'Featurising data for seeds {fold_input.rng_seeds} took '
        f' {time.time() - featurisation_start_time:.2f} seconds.'
    )
    all_inference_start_time = time.time()
    all_inference_results = []
    for seed, example in zip(fold_input.rng_seeds, featurised_examples):
        print(f'Running model inference for seed {seed}...')
        inference_start_time = time.time()
        rng_key = jax.random.PRNGKey(seed)
        result = model_runner.run_inference(example, rng_key)
        print(
            f'Running model inference for seed {seed} took '
            f' {time.time() - inference_start_time:.2f} seconds.'
        )
        print(f'Extracting output structures (one per sample) for seed {seed}...')
        extract_structures = time.time()
        inference_results = model_runner.extract_structures(
            batch=example, result=result, target_name=fold_input.name
        )
        print(
            f'Extracting output structures (one per sample) for seed {seed} took '
            f' {time.time() - extract_structures:.2f} seconds.'
        )
        all_inference_results.append(
            ResultsForSeed(
                seed=seed,
                inference_results=inference_results,
                full_fold_input=fold_input,
            )
        )
        print(
            'Running model inference and extracting output structures for seed'
            f' {seed} took  {time.time() - inference_start_time:.2f} seconds.'
        )
    print(
        'Running model inference and extracting output structures for seeds'
        f' {fold_input.rng_seeds} took '
        f' {time.time() - all_inference_start_time:.2f} seconds.'
    )
    return all_inference_results


def write_fold_input_json(
    fold_input: folding_input.Input,
    output_dir: os.PathLike[str] | str,
) -> None:
    """Writes the input JSON to the output directory."""
    os.makedirs(output_dir, exist_ok=True)
    with open(
        os.path.join(output_dir, f'{fold_input.sanitised_name()}_data.json'), 'wt'
    ) as f:
        f.write(fold_input.to_json())


def write_outputs(
    all_inference_results: Sequence[ResultsForSeed],
    output_dir: os.PathLike[str] | str,
    job_name: str,
) -> None:
    """Writes outputs to the specified output directory."""
    ranking_scores = []
    max_ranking_score = None
    max_ranking_result = None

    output_terms = (
        pathlib.Path(alphafold3.cpp.__file__).parent / 'OUTPUT_TERMS_OF_USE.md'
    ).read_text()

    os.makedirs(output_dir, exist_ok=True)
    for results_for_seed in all_inference_results:
        seed = results_for_seed.seed
        for sample_idx, result in enumerate(results_for_seed.inference_results):
            sample_dir = os.path.join(output_dir, f'seed-{seed}_sample-{sample_idx}')
            os.makedirs(sample_dir, exist_ok=True)
            post_processing.write_output(
                inference_result=result, output_dir=sample_dir
            )
            ranking_score = float(result.metadata['ranking_score'])
            ranking_scores.append((seed, sample_idx, ranking_score))
            if max_ranking_score is None or ranking_score > max_ranking_score:
                max_ranking_score = ranking_score
                max_ranking_result = result

    if max_ranking_result is not None:  # True iff ranking_scores non-empty.
        post_processing.write_output(
            inference_result=max_ranking_result,
            output_dir=output_dir,
            # The output terms of use are the same for all seeds/samples.
            terms_of_use=output_terms,
            name=job_name,
        )
        # Save csv of ranking scores with seeds and sample indices, to allow easier
        # comparison of ranking scores across different runs.
        with open(os.path.join(output_dir, 'ranking_scores.csv'), 'wt') as f:
            writer = csv.writer(f)
            writer.writerow(['seed', 'sample', 'ranking_score'])
            writer.writerows(ranking_scores)


def replace_db_dir(path_with_db_dir: str, db_dirs: Sequence[str]) -> str:
    """Replaces the DB_DIR placeholder in a path with the given DB_DIR."""
    template = string.Template(path_with_db_dir)
    if 'DB_DIR' in template.get_identifiers():
        for db_dir in db_dirs:
            path = template.substitute(DB_DIR=db_dir)
            if os.path.exists(path):
                return path
        raise FileNotFoundError(
            f'{path_with_db_dir} with ${{DB_DIR}} not found in any of {db_dirs}.'
        )
    if not os.path.exists(path_with_db_dir):
        raise FileNotFoundError(f'{path_with_db_dir} does not exist.')
    return path_with_db_dir


@overload
def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: None,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> folding_input.Input:
    ...


@overload
def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: ModelRunner,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> Sequence[ResultsForSeed]:
    ...


def process_fold_input(
    fold_input: folding_input.Input,
    data_pipeline_config: pipeline.DataPipelineConfig | None,
    model_runner: ModelRunner | None,
    output_dir: os.PathLike[str] | str,
    buckets: Sequence[int] | None = None,
) -> folding_input.Input | Sequence[ResultsForSeed]:
    """Runs data pipeline and/or inference on a single fold input.

    Args:
        fold_input: Fold input to process.
        data_pipeline_config: Data pipeline config to use. If None, skip the data
        pipeline.
        model_runner: Model runner to use. If None, skip inference.
        output_dir: Output directory to write to.
        buckets: Bucket sizes to pad the data to, to avoid excessive re-compilation
        of the model. If None, calculate the appropriate bucket size from the
        number of tokens. If not None, must be a sequence of at least one integer,
        in strictly increasing order. Will raise an error if the number of tokens
        is more than the largest bucket size.

    Returns:
        The processed fold input, or the inference results for each seed.

    Raises:
        ValueError: If the fold input has no chains.
    """
    print(f'Processing fold input {fold_input.name}')

    if not fold_input.chains:
        raise ValueError('Fold input has no chains.')

    if os.path.exists(output_dir) and os.listdir(output_dir):
        new_output_dir = (
            f'{output_dir}_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}'
        )
        print(
            f'Output directory {output_dir} exists and non-empty, using instead '
            f' {new_output_dir}.'
        )
        output_dir = new_output_dir

    if model_runner is not None:
        # If we're running inference, check we can load the model parameters before
        # (possibly) launching the data pipeline.
        print('Checking we can load the model parameters...')
        _ = model_runner.model_params

    if data_pipeline_config is None:
        print('Skipping data pipeline...')
    else:
        print('Running data pipeline...')
        fold_input = pipeline.DataPipeline(data_pipeline_config).process(fold_input)

    print(f'Output directory: {output_dir}')
    print(f'Writing model input JSON to {output_dir}')
    write_fold_input_json(fold_input, output_dir)
    if model_runner is None:
        print('Skipping inference...')
        output = fold_input
    else:
        print(
            f'Predicting 3D structure for {fold_input.name} for seed(s)'
            f' {fold_input.rng_seeds}...'
        )
        all_inference_results = predict_structure(
            fold_input=fold_input,
            model_runner=model_runner,
            buckets=buckets,
        )
        print(
            f'Writing outputs for {fold_input.name} for seed(s)'
            f' {fold_input.rng_seeds}...'
        )
        write_outputs(
            all_inference_results=all_inference_results,
            output_dir=output_dir,
            job_name=fold_input.sanitised_name(),
        )
        output = all_inference_results

    print(f'Done processing fold input {fold_input.name}.')
    return output


def main(args_dict: Dict[str, Any]) -> None:
    if args_dict['jax_compilation_cache_dir'] is not None:
        jax.config.update(
            'jax_compilation_cache_dir', args_dict['jax_compilation_cache_dir']
        )

    if args_dict["json_path"] is None == args_dict["input_dir"] is None:
        raise ValueError(
            'Exactly one of --json_path or --input_dir must be specified.'
        )

    if not args_dict["run_inference"] and not args_dict["run_data_pipeline"]:
        raise ValueError(
            'At least one of --run_inference or --run_data_pipeline must be'
            ' set to true.'
        )

    # Make sure we can create the output directory before running anything.
    try:
        os.makedirs(args_dict["output_dir"], exist_ok=True)
    except OSError as e:
        print(f'Failed to create output directory {args_dict["output_dir"]}: {e}')
        raise

    if args_dict["input_dir"] is not None:
        fold_inputs = load_fold_inputs_from_dir(
            pathlib.Path(args_dict["input_dir"]),
            run_mmseqs=args_dict["run_mmseqs"],
            output_dir=args_dict["output_dir"],
            max_template_date=args_dict["max_template_date"]
        )
    elif args_dict["json_path"] is not None:
        fold_inputs = load_fold_inputs_from_path(
            pathlib.Path(args_dict["json_path"]),
            run_mmseqs=args_dict["run_mmseqs"],
            output_dir=args_dict["output_dir"],
            max_template_date=args_dict["max_template_date"]
        )
    else:
        raise AssertionError(
            'Exactly one of --json_path or --input_dir must be specified.'
        )

    if args_dict["run_inference"]:
        # Fail early on incompatible devices, but only if we're running inference.
        gpu_devices = jax.local_devices(backend='gpu')
        if gpu_devices:
            compute_capability = float(gpu_devices[0].compute_capability)
            if compute_capability < 6.0:
                raise ValueError(
                    'AlphaFold 3 requires at least GPU compute capability 6.0 (see'
                    ' https://developer.nvidia.com/cuda-gpus).'
                )
            elif 7.0 <= compute_capability < 8.0:
                xla_flags = os.environ.get('XLA_FLAGS')
                required_flag = '--xla_disable_hlo_passes=custom-kernel-fusion-rewriter'
                if not xla_flags or required_flag not in xla_flags:
                    raise ValueError(
                        'For devices with GPU compute capability 7.x (see'
                        ' https://developer.nvidia.com/cuda-gpus), you must include'
                        ' the --cuda_compute_7x flag.'
                    )

    notice = textwrap.wrap(
        'Running AlphaFold 3. Please note that standard AlphaFold 3 model'
        ' parameters are only available under terms of use provided at'
        ' https://github.com/google-deepmind/alphafold3/blob/main/WEIGHTS_TERMS_OF_USE.md.'
        ' If you do not agree to these terms and are using AlphaFold 3 derived'
        ' model parameters, cancel execution of AlphaFold 3 inference with'
        ' CTRL-C, and do not use the model parameters.',
        break_long_words=False,
        break_on_hyphens=False,
        width=80,
    )
    print('\n'.join(notice))

    if args_dict["run_data_pipeline"]:
        # We skip this (by setting run_data_pipeline=False) since we handle MSAs
        # and templates differently.
        expand_path = lambda x: replace_db_dir(x, DB_DIR.value)
        data_pipeline_config = pipeline.DataPipelineConfig(
            jackhmmer_binary_path=_JACKHMMER_BINARY_PATH.value,
            nhmmer_binary_path=_NHMMER_BINARY_PATH.value,
            hmmalign_binary_path=_HMMALIGN_BINARY_PATH.value,
            hmmsearch_binary_path=_HMMSEARCH_BINARY_PATH.value,
            hmmbuild_binary_path=_HMMBUILD_BINARY_PATH.value,
            small_bfd_database_path=expand_path(_SMALL_BFD_DATABASE_PATH.value),
            mgnify_database_path=expand_path(_MGNIFY_DATABASE_PATH.value),
            uniprot_cluster_annot_database_path=expand_path(
                _UNIPROT_CLUSTER_ANNOT_DATABASE_PATH.value
            ),
            uniref90_database_path=expand_path(_UNIREF90_DATABASE_PATH.value),
            ntrna_database_path=expand_path(_NTRNA_DATABASE_PATH.value),
            rfam_database_path=expand_path(_RFAM_DATABASE_PATH.value),
            rna_central_database_path=expand_path(_RNA_CENTRAL_DATABASE_PATH.value),
            pdb_database_path=expand_path(_PDB_DATABASE_PATH.value),
            seqres_database_path=expand_path(_SEQRES_DATABASE_PATH.value),
            jackhmmer_n_cpu=_JACKHMMER_N_CPU.value,
            nhmmer_n_cpu=_NHMMER_N_CPU.value,
        )
    else:
        print('Skipping running the data pipeline.')
        data_pipeline_config = None

    if args_dict["run_inference"]:
        devices = jax.local_devices(backend='gpu')
        print(f'Found local devices: {devices}')

        print('Building model from scratch...')
        model_runner = ModelRunner(
            model_class=diffusion_model.Diffuser,
            config=make_model_config(
                flash_attention_implementation=typing.cast(
                    attention.Implementation, args_dict["flash_attention_implementation"]
                ),
                num_diffusion_samples=args_dict["num_diffusion_samples"],
            ),
            device=devices[0],
            model_dir=pathlib.Path(args_dict["model_dir"]),
        )
    else:
        print('Skipping running model inference.')
        model_runner = None

    print('Processing fold inputs.')
    num_fold_inputs = 0
    for fold_input in fold_inputs:
        print(f'Processing fold input #{num_fold_inputs + 1}')
        process_fold_input(
            fold_input=fold_input,
            data_pipeline_config=data_pipeline_config,
            model_runner=model_runner,
            output_dir=os.path.join(args_dict["output_dir"], fold_input.sanitised_name()),
            buckets=tuple(int(bucket) for bucket in args_dict["buckets"]),
        )
        num_fold_inputs += 1

    print(f'Done processing {num_fold_inputs} fold inputs.')


if __name__ == '__main__':
    # Work around for a known XLA issue:
    # https://github.com/google-deepmind/alphafold3/blob/main/docs/performance.md#compilation-time-workaround-with-xla-flags
    os.environ["XLA_FLAGS"] = "--xla_gpu_enable_triton_gemm=false"

    args_dict = get_af3_args()

    # Work around for a known XLA issue:
    # https://github.com/google-deepmind/alphafold3/blob/main/docs/performance.md#compilation-time-workaround-with-xla-flags
    os.environ["XLA_FLAGS"] = "--xla_gpu_enable_triton_gemm=false"

    # Add required flag for CUDA compute capability 7.x
    if args_dict["cuda_compute_7x"]:
        os.environ["XLA_FLAGS"] = "--xla_disable_hlo_passes=custom-kernel-fusion-rewriter"

    main(args_dict)
