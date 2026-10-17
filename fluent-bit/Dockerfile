FROM fluent/fluent-bit:5.1.4

COPY fluent-bit.yaml parsers.yaml /fluent-bit/etc/

CMD ["--config=/fluent-bit/etc/fluent-bit.yaml"]
