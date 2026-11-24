"""Generate the catalog XML files used for the scale measurements."""
import os
import sys


def write_catalog(path: str, n_items: int, interleave: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fp.write('<catalog xmlns="http://example.com/catalog" version="1.0">\n')
        for i in range(n_items):
            if interleave and i:
                # same-name items separated by a different element
                fp.write(f'  <separator n="{i}"/>\n')
            fp.write(f'  <item id="id-{i:06d}">\n')
            fp.write(f'    <code>{i}</code>\n')
            fp.write(f'    <name>Item number {i} with some padding text</name>\n')
            fp.write(f'    <price>{i}.99</price>\n')
            fp.write(f'    <in_stock>{str(i % 2 == 0).lower()}</in_stock>\n')
            fp.write(f'    <tag>tag-{i % 7}</tag>\n')
            fp.write(f'    <tag>tag-{i % 13}</tag>\n')
            fp.write('  </item>\n')
        fp.write('</catalog>\n')


def write_repeated_schema(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('''<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:ns="http://example.com/catalog"
           targetNamespace="http://example.com/catalog"
           elementFormDefault="qualified">
  <xs:element name="catalog" type="ns:catalogType"/>
  <xs:complexType name="catalogType">
    <xs:sequence>
      <xs:element name="item" type="ns:itemType" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="version" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:integer"/>
      <xs:element name="name" type="xs:string"/>
      <xs:element name="price" type="xs:decimal"/>
      <xs:element name="in_stock" type="xs:boolean" minOccurs="0"/>
      <xs:element name="tag" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
</xs:schema>
''')


def write_interleave_schema(path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fp:
        fp.write('''<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:ns="http://example.com/catalog"
           targetNamespace="http://example.com/catalog"
           elementFormDefault="qualified">
  <xs:element name="catalog" type="ns:catalogType"/>
  <xs:complexType name="catalogType">
    <xs:sequence>
      <xs:choice minOccurs="0" maxOccurs="unbounded">
        <xs:element name="item" type="ns:itemType"/>
        <xs:element name="separator">
          <xs:complexType>
            <xs:attribute name="n" type="xs:integer"/>
          </xs:complexType>
        </xs:element>
      </xs:choice>
    </xs:sequence>
    <xs:attribute name="version" type="xs:string"/>
  </xs:complexType>
  <xs:complexType name="itemType">
    <xs:sequence>
      <xs:element name="code" type="xs:integer"/>
      <xs:element name="name" type="xs:string"/>
      <xs:element name="price" type="xs:decimal"/>
      <xs:element name="in_stock" type="xs:boolean" minOccurs="0"/>
      <xs:element name="tag" type="xs:string" minOccurs="0" maxOccurs="unbounded"/>
    </xs:sequence>
    <xs:attribute name="id" type="xs:string" use="required"/>
  </xs:complexType>
</xs:schema>
''')


if __name__ == '__main__':
    out_dir = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100_000
    write_repeated_schema(os.path.join(out_dir, 'catalog.xsd'))
    write_interleave_schema(os.path.join(out_dir, 'catalog-interleave.xsd'))
    write_catalog(os.path.join(out_dir, 'catalog-10k.xml'), 10_000)
    write_catalog(os.path.join(out_dir, 'catalog-100k.xml'), n)
    write_catalog(os.path.join(out_dir, 'catalog-interleaved-100k.xml'),
                  n, interleave=True)
    print('generated into', out_dir)
