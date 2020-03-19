

import numpy as np
cimport numpy as np
cimport cython

ctypedef np.int_t DTYPE_it
ctypedef np.float_t DTYPE_ft

@cython.boundscheck(False) # turn off bounds-checking for entire function
@cython.wraparound(False)  # turn off negative index wrapping for entire function
def two_opt_swap(np.ndarray[DTYPE_it, ndim=1] route, int i, int k):
    cdef int ind, ind1
    cdef int route_length = route.shape[0]

    new_route = np.empty_like(route)

    for ind in range(i):
        # [start up to first swap position)
        new_route[ind] = route[ind]

    for ind, ind1 in zip(range(i, k + 1), range(k, i - 1, -1)):
        # [from first swap to second], reversed
        new_route[ind] = route[ind1]

    for ind in range(k + 1, route_length):
        new_route[ind] = route[ind]

    return new_route

def two_opt_test(np.ndarray[DTYPE_it, ndim=1] route, int i, int k, np.ndarray[DTYPE_ft, ndim=2] distances, int k_max):
    cdef float removed = 0
    cdef float added = 0

    if i > 0:
        removed = distances[route[i - 1], route[i]]
        added = distances[route[i - 1], route[k]]

    if k < k_max:
        removed += distances[route[k], route[k + 1]]
        added += distances[route[i], route[k + 1]]

    return added - removed