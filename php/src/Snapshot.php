<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/**
 * A document snapshot: identity, existence, data.
 *
 * The only surface a well-behaved consumer reads, and therefore the only surface the tape
 * records. A store client hands back objects with a dozen methods on them; recording their
 * shape would be recording the client library rather than the answer it gave.
 */
final class Snapshot implements \JsonSerializable
{
    public function __construct(
        public readonly ?string $id,
        public readonly bool $exists,
        public readonly mixed $data = null,
    ) {
    }

    /** Revive a snapshot off the tape. */
    public static function fromArray(array $a): self
    {
        return new self(
            isset($a['id']) ? (string) $a['id'] : null,
            (bool) ($a['exists'] ?? false),
            Serial::fromJsonable($a['data'] ?? null),
        );
    }

    /** A missing document. Its `data` is null, and reading it is the caller's business. */
    public static function missing(?string $id = null): self
    {
        return new self($id, false, null);
    }

    /** The document's fields, or an empty array when it does not exist. */
    public function toArray(): array
    {
        return is_array($this->data) ? $this->data : [];
    }

    /** One field, or `$default` when the document or the field is absent. */
    public function get(string $field, mixed $default = null): mixed
    {
        return is_array($this->data) && array_key_exists($field, $this->data)
            ? $this->data[$field]
            : $default;
    }

    public function jsonSerialize(): array
    {
        return Serial::snapshotJsonable($this);
    }
}
