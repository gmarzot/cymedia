# cymedia

Low-latency, group-aligned media processing graphs on FFmpeg's libraries, for Python.

A graph decodes a source once, then feeds any number of branches. Each branch is a filter chain
ending in an encoder, and each branch's output goes to a sink. Source group numbers and timestamps
pass through unchanged, and every branch starts a new keyframe at each source group start, so
equally numbered groups line up across all outputs. The first graph is an ABR ladder.

Status: early development.
