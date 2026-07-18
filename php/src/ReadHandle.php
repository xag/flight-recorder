<?php

declare(strict_types=1);

namespace Xag\FlightRecorder;

/** One recorded chained read, editable. */
final class ReadHandle
{
    /** @param array<string, mixed> $ev */
    public function __construct(
        private readonly MutateHandle $owner,
        private readonly int $index,
        private array $ev,
    ) {
    }

    public function result(): mixed
    {
        return Serial::fromJsonable($this->ev['res'] ?? null);
    }

    /**
     * Replace what the read returned, wrapping bare values as snapshots.
     *
     * This is convenience, not magic: writing the `{id, exists, data}` wrapper by hand every time
     * is how a probe session becomes tedious enough that nobody runs one.
     */
    public function setResult(mixed $value): self
    {
        $this->ev['res'] = self::wrap($value);
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }

    /** The corpus is gone, the row is missing, the list came back empty. */
    public function setEmpty(): self
    {
        $this->ev['res'] = [];
        $this->owner->setEvent($this->index, $this->ev);
        return $this;
    }

    private static function wrap(mixed $value): mixed
    {
        if ($value instanceof Snapshot) {
            return Serial::snapshotJsonable($value);
        }
        if (is_array($value) && array_is_list($value)) {
            $out = [];
            foreach ($value as $i => $row) {
                $out[] = self::one($row, "row$i");
            }
            return $out;
        }
        return self::one($value, 'row0');
    }

    private static function one(mixed $row, string $id): array
    {
        if ($row instanceof Snapshot) {
            return Serial::snapshotJsonable($row);
        }
        if (is_array($row)
            && array_key_exists('id', $row)
            && array_key_exists('exists', $row)
            && array_key_exists('data', $row)) {
            return $row; // already a snapshot on the wire
        }
        return ['id' => $id, 'exists' => true, 'data' => Serial::toJsonable($row)];
    }
}
