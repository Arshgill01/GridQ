import os

# Single-threaded BLAS/OpenMP: GridQ parallelises across processes (benchmark) and runs on a
# 4-core board next to a web server; per-tree OpenMP fan-out made GBM inference ~50x slower.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
