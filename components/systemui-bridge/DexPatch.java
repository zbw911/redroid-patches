import com.android.tools.smali.dexlib2.DexFileFactory;
import com.android.tools.smali.dexlib2.Opcodes;
import com.android.tools.smali.dexlib2.Opcode;
import com.android.tools.smali.dexlib2.builder.MutableMethodImplementation;
import com.android.tools.smali.dexlib2.builder.instruction.BuilderInstruction3rc;
import com.android.tools.smali.dexlib2.builder.instruction.BuilderInstruction35c;
import com.android.tools.smali.dexlib2.immutable.ImmutableClassDef;
import com.android.tools.smali.dexlib2.immutable.ImmutableMethod;
import com.android.tools.smali.dexlib2.immutable.reference.ImmutableMethodReference;
import com.android.tools.smali.dexlib2.writer.pool.DexPool;
import com.android.tools.smali.dexlib2.writer.io.FileDataStore;
import com.android.tools.smali.dexlib2.iface.*;
import com.android.tools.smali.dexlib2.iface.instruction.*;
import com.android.tools.smali.dexlib2.formatter.DexFormatter;
import java.io.File;
import java.util.*;

public final class DexPatch {
    static final String TARGET = "Lcom/android/systemui/statusbar/CommandQueue;";
    static final String EXPECTED = TARGET + "->onSystemBarAttributesChanged(II[Lcom/android/internal/view/AppearanceRegion;ZIILjava/lang/String;[Lcom/android/internal/statusbar/LetterboxDetails;)V";
    static final String START = "Lcom/android/systemui/statusbar/phone/CentralSurfacesImpl;->start()V";
    static final String REGISTER = "Lcom/android/internal/statusbar/IStatusBarService;->registerStatusBar(Lcom/android/internal/statusbar/IStatusBar;)Lcom/android/internal/statusbar/RegisterStatusBarResult;";
    static final String TASK_REPLAY = "Lcom/android/wm/shell/ShellTaskOrganizer;->registerOrganizer()Ljava/util/List;";
    static final String TASK_APPEARED = "Lcom/android/wm/shell/ShellTaskOrganizer;->onTaskAppeared(Landroid/window/TaskAppearedInfo;)V";
    static String signature(Method method) {
        return method.getDefiningClass() + "->" + method.getName() + "("
                + String.join("", method.getParameterTypes()) + ")" + method.getReturnType();
    }
    public static void main(String[] args) throws Exception {
        if (args.length != 2) throw new IllegalArgumentException("Usage: DexPatch <source.apk> <patched.dex>");
        // Match this frozen APK's dex039 format. API35 selects 041 in dexlib, whose writer
        // does not produce the larger container header required by ART for that version.
        Opcodes originalFormat = Opcodes.forDexVersion(39);
        MultiDexContainer<?> container = DexFileFactory.loadDexContainer(new File(args[0]), originalFormat);
        int matched = 0, registrations = 0, taskRestores = 0;
        for (String entry : container.getDexEntryNames()) {
            DexFile dex = container.getEntry(entry).getDexFile();
            List<ClassDef> classes = new ArrayList<>();
            boolean modified = false;
            for (ClassDef cls : dex.getClasses()) {
                if (!TARGET.equals(cls.getType())
                        && !"Lcom/android/systemui/statusbar/phone/CentralSurfacesImpl;".equals(cls.getType())
                        && !"Lcom/android/wm/shell/ShellTaskOrganizer;".equals(cls.getType())) {
                    classes.add(cls); continue;
                }
                boolean classModified = false;
                List<Method> methods = new ArrayList<>();
                for (Method method : cls.getVirtualMethods()) {
                    if (signature(method).equals(START) || signature(method).equals(TASK_REPLAY)) {
                        boolean registration = signature(method).equals(START);
                        String callee = registration ? REGISTER : TASK_APPEARED;
                        int expectedRegisters = registration ? 18 : 10;
                        int expectedUnits = registration ? 2600 : 92;
                        int address = registration ? 205 : 82;
                        int receiver = registration ? 0 : 9;
                        int argument = registration ? 7 : 4;
                        Opcode opcode = registration ? Opcode.INVOKE_INTERFACE : Opcode.INVOKE_VIRTUAL;
                        MethodImplementation original = method.getImplementation();
                        MutableMethodImplementation impl = new MutableMethodImplementation(original);
                        int units = 0, calls = 0;
                        for (int index = 0; index < impl.getInstructions().size(); index++) {
                            Instruction instruction = impl.getInstructions().get(index);
                            units += instruction.getCodeUnits();
                            if (!(instruction instanceof ReferenceInstruction)
                                    || !callee.equals(DexFormatter.INSTANCE.getReference(
                                            ((ReferenceInstruction) instruction).getReference()))) continue;
                            if (instruction.getOpcode() != opcode
                                    || !(instruction instanceof FiveRegisterInstruction))
                                throw new IllegalStateException("Pinned startup call opcode changed");
                            FiveRegisterInstruction call = (FiveRegisterInstruction) instruction;
                            if (call.getRegisterCount() != 2 || call.getRegisterC() != receiver
                                    || call.getRegisterD() != argument || units - instruction.getCodeUnits() != address)
                                throw new IllegalStateException("Pinned startup call operands changed");
                            ImmutableMethodReference bridge = new ImmutableMethodReference(
                                    "Lcom/buildploy/statusbarprototype/AppearanceBridge;",
                                    registration ? "register" : "existingTaskAppeared",
                                    Arrays.asList("Ljava/lang/Object;", "Ljava/lang/Object;"),
                                    registration ? "Lcom/android/internal/statusbar/RegisterStatusBarResult;" : "V");
                            // Same-sized call and result type retain every original branch/try address.
                            impl.replaceInstruction(index, new BuilderInstruction35c(
                                    Opcode.INVOKE_STATIC, 2, receiver, argument, 0, 0, 0, bridge));
                            calls++;
                        }
                        if (original.getRegisterCount() != expectedRegisters || units != expectedUnits || calls != 1)
                            throw new IllegalStateException("Pinned SystemUI startup method changed");
                        if (registration) registrations++; else taskRestores++;
                        String prefix = registration ? "registration" : "task_restore";
                        System.out.println(prefix + "_method=" + signature(method));
                        System.out.println(prefix + "_registers=" + original.getRegisterCount());
                        System.out.println(prefix + "_code_units=" + units);
                        System.out.println(prefix + "_instruction_address=" + address);
                        methods.add(new ImmutableMethod(method.getDefiningClass(), method.getName(),
                                method.getParameters(), method.getReturnType(), method.getAccessFlags(),
                                method.getAnnotations(), method.getHiddenApiRestrictions(), impl));
                        modified = classModified = true;
                        continue;
                    }
                    if (!signature(method).equals(EXPECTED)) { methods.add(method); continue; }
                    matched++;
                    int count = 0;
                    for (Object ignored : method.getImplementation().getInstructions()) count++;
                    System.out.println("entry=" + entry);
                    System.out.println("method=" + signature(method));
                    System.out.println("registers=" + method.getImplementation().getRegisterCount());
                    System.out.println("instructions=" + count);
                    if (count != 22 || method.getImplementation().getRegisterCount() != 11)
                        throw new IllegalStateException("Original callback instructions/registers differ from pinned input");
                    MutableMethodImplementation impl = new MutableMethodImplementation(method.getImplementation());
                    ImmutableMethodReference bridge = new ImmutableMethodReference(
                            "Lcom/buildploy/statusbarprototype/AppearanceBridge;", "dispatch",
                            Arrays.asList("I", "I", "Ljava/lang/Object;", "Z", "I", "I", "Ljava/lang/String;", "Ljava/lang/Object;"), "V");
                    impl.addInstruction(0, new BuilderInstruction3rc(Opcode.INVOKE_STATIC_RANGE,
                            impl.getRegisterCount() - 8, 8, bridge));
                    methods.add(new ImmutableMethod(method.getDefiningClass(), method.getName(),
                            method.getParameters(), method.getReturnType(), method.getAccessFlags(),
                            method.getAnnotations(), method.getHiddenApiRestrictions(), impl));
                    modified = classModified = true;
                }
                classes.add(classModified ? new ImmutableClassDef(cls.getType(), cls.getAccessFlags(),
                        cls.getSuperclass(), cls.getInterfaces(), cls.getSourceFile(), cls.getAnnotations(),
                        cls.getStaticFields(), cls.getInstanceFields(), cls.getDirectMethods(), methods) : cls);
            }
            if (modified) {
                DexPool pool = new DexPool(originalFormat);
                for (ClassDef cls : classes) pool.internClass(cls);
                FileDataStore store = new FileDataStore(new File(args[1]));
                try { pool.writeTo(store); } finally { store.close(); }
                System.out.println("patched=true");
            }
        }
        if (matched != 1) throw new IllegalStateException("Expected exactly one target, got " + matched);
        if (registrations != 1) throw new IllegalStateException("Expected exactly one registration, got " + registrations);
        if (taskRestores != 1) throw new IllegalStateException("Expected exactly one task replay call, got " + taskRestores);
    }
}
