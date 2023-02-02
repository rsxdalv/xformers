# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

# Generates combination of kernels - implementations and registry

# Kernels are ordered (see `sort_index`), and when dispatching,
# we select the first kernel in the list that supports the inputs

import collections
import itertools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, TypeVar

DTYPES = {
    "f32": "float",
    "f16": "cutlass::half_t",
    "bf16": "cutlass::bfloat16_t",
}

SM = [50, 70, 75, 80]

KERNEL_IMPL_TEMPLATE = """__global__ void __launch_bounds__(
    {CPP_CLASS}::kNumThreads,
    {CPP_CLASS}::kMinBlocksPerSm)
{NAME}(typename {CPP_CLASS}::Params p) {{
#ifdef __CUDA_ARCH__
#if __CUDA_ARCH__ >= {SM}0
#if __CUDA_ARCH__ < {SM_MAX}0
  if (!p.advance_to_block()) {{
    return;
  }}
  {CPP_CLASS}::attention_kernel(p);
  return;
#endif
#endif
    printf(
        "FATAL: kernel `{NAME}` is for sm{SM}-sm{SM_MAX}, but was built for sm%d\\n",
        int(__CUDA_ARCH__ + 0) / 10);
#endif
}}
"""


@dataclass(order=True)
class FwdKernel:
    sort_index: Tuple[int, ...] = field(init=False, repr=False)
    aligned: bool
    dtype: str
    sm: int
    sm_max: int
    q: int
    k: int
    single_value_iter: bool
    supports_dropout: bool = True
    supports_bias: bool = True

    def __post_init__(self) -> None:
        # Set kernel selection priority
        # The lowest value that matches inputs
        # will be selected
        self.sort_index = (
            # First select aligned kernel
            0 if self.aligned else 1,
            # Then keep output in RF
            0 if self.single_value_iter else 1,
            self.k,
            # Prefer kernels without dropout/bias if available
            1 if self.supports_dropout else 0,
            1 if self.supports_bias else 0,
        )

    @property
    def _aligned_suffix(self) -> str:
        return "aligned" if self.aligned else "notaligned"

    @property
    def name(self) -> str:
        acc = "rf" if self.single_value_iter else "gmem"
        return f"fmha_cutlassF_{self.dtype}_{self._aligned_suffix}_{self.q}x{self.k}_{acc}_sm{self.sm}"

    @property
    def cpp_class(self) -> str:
        template_args = ", ".join(
            [
                DTYPES[self.dtype],
                f"cutlass::arch::Sm{self.sm}",
                "true" if self.aligned else "false",
                str(self.q),
                str(self.k),
                "true" if self.single_value_iter else "false",
                "true" if self.supports_dropout else "false",
                "true" if self.supports_bias else "false",
            ]
        )
        return f"AttentionKernel<{template_args}>"

    @property
    def impl_group(self) -> str:
        # Maps to file which will contain the implementation
        return f"{self.dtype}_{self._aligned_suffix}"

    @property
    def cpp_impl(self) -> str:
        return KERNEL_IMPL_TEMPLATE.format(
            CPP_CLASS=self.cpp_class,
            NAME=self.name,
            SM=self.sm,
            SM_MAX=self.sm_max,
        )

    @classmethod
    def get_all(cls) -> List["FwdKernel"]:
        kernels: List[FwdKernel] = []
        for aligned, dtype, (sm, sm_max) in itertools.product(
            [True, False], DTYPES.keys(), zip(SM, SM[1:] + [90])
        ):
            # Remove some kernels we don't use
            if dtype == "bf16" and sm < 80:
                continue
            if not aligned and sm >= 80:
                continue
            for q, k, single_value_iter in [
                (32, 128, True),
                (32, 128, False),
                (64, 64, True),
            ]:
                kernels.append(
                    cls(
                        aligned=aligned,
                        dtype=dtype,
                        sm=sm,
                        sm_max=sm_max,
                        q=q,
                        k=k,
                        single_value_iter=single_value_iter,
                    )
                )
        return kernels


@dataclass(order=True)
class BwdKernel:
    sort_index: Tuple[int, ...] = field(init=False, repr=False)
    sm: int
    sm_max: int
    dtype: str
    aligned: bool
    apply_dropout: bool
    max_k: int

    def __post_init__(self) -> None:
        # Set kernel selection priority
        # The lowest value that matches inputs
        # will be selected
        self.sort_index = (
            # First select aligned kernel
            0 if self.aligned else 1,
            # Take a kernel without dropout if possible
            1 if self.apply_dropout else 0,
            # Then take the smallest maxK
            self.max_k,
        )

    @property
    def _aligned_suffix(self) -> str:
        return "aligned" if self.aligned else "notaligned"

    @property
    def name(self) -> str:
        dropout_suffix = "_dropout" if self.apply_dropout else ""
        return f"fmha_cutlassB_{self.dtype}_{self._aligned_suffix}_k{self.max_k}{dropout_suffix}_sm{self.sm}"

    @property
    def cpp_class(self) -> str:
        template_args = ", ".join(
            [
                f"cutlass::arch::Sm{self.sm}",
                DTYPES[self.dtype],
                "true" if self.aligned else "false",
                "true" if self.apply_dropout else "false",
                str(self.max_k),
            ]
        )
        return f"AttentionBackwardKernel<{template_args}>"

    @property
    def impl_group(self) -> str:
        # Maps to file which will contain the implementation
        dropout_suffix = "_dropout" if self.apply_dropout else ""
        return f"{self.dtype}_{self._aligned_suffix}_k{self.max_k}{dropout_suffix}"

    @property
    def cpp_impl(self) -> str:
        return KERNEL_IMPL_TEMPLATE.format(
            CPP_CLASS=self.cpp_class,
            NAME=self.name,
            SM=self.sm,
            SM_MAX=self.sm_max,
        )

    @classmethod
    def get_all(cls) -> List["BwdKernel"]:
        kernels: List[BwdKernel] = []
        for aligned, dtype, (sm, sm_max), apply_dropout, max_k in itertools.product(
            [True, False],
            DTYPES.keys(),
            zip(SM, SM[1:] + [90]),
            [True, False],
            [32, 64, 128, 2**16],
        ):
            if dtype == "bf16" and sm < 80:
                continue
            if not aligned and sm >= 80:
                continue
            kernels.append(
                cls(
                    aligned=aligned,
                    dtype=dtype,
                    sm=sm,
                    sm_max=sm_max,
                    apply_dropout=apply_dropout,
                    max_k=max_k,
                )
            )
        return kernels


T = TypeVar("T", FwdKernel, BwdKernel)


def write_decl_impl(
    kernels: List[T], family_name: str, impl_file: str, disable_def: str
) -> None:
    cpp_file_header = f"""// This file is auto-generated. See "generate_kernels.py"
#include "{impl_file}"

"""

    kernels.sort()

    implfile_to_kernels: Dict[str, List[T]] = collections.defaultdict(list)
    cat_to_kernels: Dict[Tuple[str, int, int], List[T]] = collections.defaultdict(list)

    dispatch_all = ""
    declarations = cpp_file_header + "#pragma once\n"
    declarations += f"#ifndef {disable_def}\n"

    # Declaration of kernel functions
    for k in kernels:
        implfile_to_kernels[k.impl_group].append(k)
        cat_to_kernels[(k.dtype, k.sm, k.sm_max)].append(k)

    for (cat_dt, cat_sm, cat_sm_max), kernels in cat_to_kernels.items():
        declarations += f"// ======== {cat_dt} / sm{cat_sm} ========\n"
        declarations += "\n".join(
            k.cpp_impl.split("{")[0].rstrip() + ";" for k in kernels
        )
        dispatch_category_fn = f"dispatch_{family_name}_{cat_dt}_sm{cat_sm}"
        declarations += (
            f"\n\ntemplate <typename T> void {dispatch_category_fn}(T cb) {{\n"
        )
        declarations += "\n".join(
            f"    cb({k.cpp_class}(), {k.name});" for k in kernels
        )
        declarations += "\n}\n"
        declarations += "\n"
        dispatch_all += f"""
    if (std::is_same<DT, {DTYPES[cat_dt]}>::value && {cat_sm} <= cc && cc < {cat_sm_max}) {{
        {dispatch_category_fn}(cb);
    }}"""

    declarations += f"""
template <typename DT, typename T>
void dispatch_{family_name}(T cb, int cc = 0) {{
{dispatch_all}
}}
"""
    declarations += f"#endif // {disable_def}\n"
    Path(f"{family_name}.h").write_text(declarations)

    for f, f_kernels in implfile_to_kernels.items():
        impl_cu = f"#ifndef {disable_def}\n{cpp_file_header}"
        for k in f_kernels:
            impl_cu += k.cpp_impl
        impl_cu += f"#endif // {disable_def}\n"
        Path(f"{family_name}_{f}.cu").write_text(impl_cu)


write_decl_impl(
    FwdKernel.get_all(),
    "cutlassF",
    impl_file="../kernel_forward.h",
    disable_def="XFORMERS_MEM_EFF_ATTENTION_DISABLE_FORWARD",
)
write_decl_impl(
    BwdKernel.get_all(),
    "cutlassB",
    impl_file="../kernel_backward.h",
    disable_def="XFORMERS_MEM_EFF_ATTENTION_DISABLE_BACKWARD",
)
