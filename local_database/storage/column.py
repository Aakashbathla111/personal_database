class Column:

    VALID_TYPES = {"INT", "TEXT", "FLOAT", "BOOL", "DATE", "BLOB"}

    def __init__(
        self,
        name,
        datatype,
        primary_key=False,
        nullable=True,
        unique=False,
        default=None,
        check=None,
        foreign_key=None,
    ):
        datatype = datatype.upper()
        if datatype not in self.VALID_TYPES:
            raise TypeError(f"Unknown type '{datatype}'")
        self.name = name
        self.datatype = datatype
        self.primary_key = primary_key
        self.nullable = nullable
        self.unique = unique
        self.default = default
        self.check = check          # e.g. "> 0"
        self.foreign_key = foreign_key  # {"table": str, "column": str}

    # ------------------------------------------------------------------ #

    def cast(self, value):
        """Cast a raw string value to the column's Python type."""
        if value is None or (isinstance(value, str) and value.upper() == "NULL"):
            return None
        if self.datatype == "INT":
            return int(value)
        if self.datatype == "FLOAT":
            return float(value)
        if self.datatype == "BOOL":
            if isinstance(value, bool):
                return value
            return str(value).upper() in ("TRUE", "1", "YES")
        if self.datatype in ("TEXT", "DATE", "BLOB"):
            return str(value)
        return value

    def to_dict(self):
        return {
            "name": self.name,
            "datatype": self.datatype,
            "primary_key": self.primary_key,
            "nullable": self.nullable,
            "unique": self.unique,
            "default": self.default,
            "check": self.check,
            "foreign_key": self.foreign_key,
        }

    @staticmethod
    def from_dict(data):
        return Column(
            name=data["name"],
            datatype=data["datatype"],
            primary_key=data.get("primary_key", False),
            nullable=data.get("nullable", True),
            unique=data.get("unique", False),
            default=data.get("default"),
            check=data.get("check"),
            foreign_key=data.get("foreign_key"),
        )

    def __repr__(self):
        flags = []
        if self.primary_key:
            flags.append("PK")
        if not self.nullable:
            flags.append("NOT NULL")
        if self.unique:
            flags.append("UNIQUE")
        if self.default is not None:
            flags.append(f"DEFAULT={self.default}")
        if self.foreign_key:
            fk = self.foreign_key
            flags.append(f"FK->{fk['table']}.{fk['column']}")
        return f"Column({self.name} {self.datatype} {' '.join(flags)})"
