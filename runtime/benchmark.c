/* Bounded CPU calibration: memory copy and dense FP32 matrix multiplication. */
#include <omp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
static volatile float sink;
static double clock_s(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
int main(int argc, char **argv) {
    int maximum = argc > 1 ? atoi(argv[1]) : omp_get_num_procs();
    if (maximum < 1) maximum = 1;
    if (maximum > 128) maximum = 128;
    const size_t bytes = 32 * 1024 * 1024;
    char *source = malloc(bytes), *dest = malloc(bytes);
    const int n = 256;
    float *a = calloc(n * n, sizeof(float)), *b = calloc(n * n, sizeof(float)), *c = calloc(n * n, sizeof(float));
    if (!source || !dest || !a || !b || !c) { free(source); free(dest); free(a); free(b); free(c); return 1; }
    memset(source, 17, bytes);
    double start = clock_s();
    for (int k = 0; k < 32; k++) { memcpy(dest, source, bytes); sink = dest[k]; }
    double bandwidth = bytes * 32 / (clock_s() - start);
    for (int i = 0; i < n * n; i++) { a[i] = (i % 31) / 31.0f; b[i] = (i % 17) / 17.0f; }
    double best = 0;
    int recommended = 1;
    for (int threads = 1; ; threads = threads * 2 > maximum ? maximum : threads * 2) {
        omp_set_num_threads(threads);
        start = clock_s();
        for (int round = 0; round < 3; round++) {
            #pragma omp parallel for schedule(static)
            for (int i = 0; i < n; i++) {
                for (int j = 0; j < n; j++) c[i * n + j] = 0;
                for (int k = 0; k < n; k++) {
                    float v = a[i * n + k];
                    for (int j = 0; j < n; j++) c[i * n + j] += v * b[k * n + j];
                }
            }
            sink = c[round];
        }
        double gflops = 6.0 * n * n * n / (clock_s() - start) / 1e9;
        if (gflops > best * 1.03) { best = gflops; recommended = threads; }
        if (threads == maximum) break;
    }
    printf("{\"memory_copy_bytes_sec\":%.0f,\"matrix_fp32_gflops\":%.4f,\"recommended_threads\":%d,\"workload\":\"32MiB copy and 256x256 dense FP32 GEMM; calibration, not token/s prediction\"}\n", bandwidth, best, recommended);
    free(source); free(dest); free(a); free(b); free(c);
    return 0;
}
